/******************************************************************************
* Freematics Hub Server
* Developed by Stanley Huang <stanley@freematics.com.au>
* Distributed under GPL v3.0 license
* Visit https://freematics.com/hub for more information
*
* THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
* IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
* FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
* AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
* LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
* OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
* THE SOFTWARE.
******************************************************************************/

#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <stdint.h>
#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <limits.h>
#include <stdarg.h>
#include <sys/stat.h>
#include "data2kml.h"
#include "httpd.h"
#include "teleserver.h"
#include "logdata.h"
#include "processpil.h"
#include "revision.h"

int uhPush(UrlHandlerParam* param);
int uhPull(UrlHandlerParam* param);
int uhGet(UrlHandlerParam* param);
int uhPost(UrlHandlerParam* param);
int uhChannels(UrlHandlerParam* param);
int uhChannelsXML(UrlHandlerParam* param);
int uhNotify(UrlHandlerParam* param);
int uhCommand(UrlHandlerParam* param);
int uhTest(UrlHandlerParam* param);
int uhMetrics(UrlHandlerParam* param);

int uhTrip(UrlHandlerParam* param);
int uhHistory(UrlHandlerParam* param);
int uhData(UrlHandlerParam* param);
int uhQuery(UrlHandlerParam* param);
int phData(void* _hp, int op, char* buf, int len);

UrlHandler urlHandlerList[]={
	{"metrics", uhMetrics},
	{"api/post", uhPost},
	{"api/push", uhPush},
	{"api/get", uhGet},
	{"api/pull", uhPull},
	{"api/notify", uhNotify },
	{"api/command", uhCommand },
	{"api/channels.xml", uhChannelsXML },
	{"api/channels", uhChannels},
	{"api/query", uhQuery},
	{"api/data", uhData},
	{"api/trip", uhTrip },
	{"api/history", uhHistory },
	{"api/test", uhTest},
	{NULL},
};

int loadConfig();

char username[64] = "admin";
char password[64] = { 0 };

AuthHandler authHandlerList[]={
	{ "", username, password },
	{NULL}
};

HttpParam httpParam;

char dataDir[256] = "data";
char logDir[256] = "log";
char serverKey[256] = { 0 };
int noGUI = 0;

CHANNEL_DATA ld[MAX_CHANNELS];

typedef struct {
	uint16_t pid;
	const char* name;
	const char* description;
	const char* unit;
} OBD_PID_META;

static const OBD_PID_META obdPidCatalog[] = {
#define OBD_PID(pid, name, description, unit, priority) {0x100 | pid, #name, description, unit},
#include "../obd_pids.h"
};

static const OBD_PID_META* findOBDPIDMeta(uint16_t pid)
{
	for (unsigned int i = 0; i < sizeof(obdPidCatalog) / sizeof(obdPidCatalog[0]); i++) {
		if (obdPidCatalog[i].pid == pid) return obdPidCatalog + i;
	}
	return NULL;
}

static int parseFiniteNumber(const char* text, double* value)
{
	if (!text || !*text) return 0;
	errno = 0;
	char* end = NULL;
	double parsed = strtod(text, &end);
	if (end == text || *end || errno == ERANGE || !isfinite(parsed)) return 0;
	if (value) *value = parsed;
	return 1;
}

static unsigned int tickAgeMs(uint64_t now, uint64_t then);
static int appendFormat(char* buf, int bs, int l, const char* format, ...)
{
	if (!buf || !format || bs <= 0 || l < 0) return 0;
	if (l >= bs - 1) return bs - 1;
	va_list args;
	va_start(args, format);
	int written = vsnprintf(buf + l, (size_t)(bs - l), format, args);
	va_end(args);
	if (written < 0) return l;
	return written >= bs - l ? bs - 1 : l + written;
}

static int appendScalarMetric(char* buf, int bs, int l, const char* metric,
	const char* devid, const char* tripid, const PID_DATA* data, double scale)
{
	if (!buf || bs <= 0 || !data || !data->ts || l < 0 || l >= bs - 1) return l;
	double value;
	if (!parseFiniteNumber(data->value, &value) || !isfinite(value * scale)) return l;
	return appendFormat(buf, bs, l, "%s{device_id=\"%s\",trip_id=\"%s\"} %.10g\n",
		metric, devid, tripid, value * scale);
}

static int getNumericPID(const CHANNEL_DATA* pld, uint16_t pid, double* value)
{
	if (!pld || !value || pid >= 256 * PID_MODES) return 0;
	const PID_DATA* data = pld->data + pid;
	if (!data->ts) return 0;
	double parsed;
	if (!parseFiniteNumber(data->value, &parsed)) return 0;
	*value = parsed;
	return 1;
}

static int appendDerivedVehicleMetrics(char* buf, int bs, int l, const CHANNEL_DATA* pld)
{
	if (!buf || !pld || bs <= 0 || l < 0) return 0;
	/* These calculations only emit when all required source values are present. */
	double rpm;
	int engineRunning = getNumericPID(pld, PID_RPM, &rpm) && rpm >= 400.0;
	if (getNumericPID(pld, PID_RPM, &rpm)) {
		l = appendFormat(buf, bs, l,
			"freematics_vehicle_engine_running{device_id=\"%s\",trip_id=\"%s\"} %d\n",
			pld->devid, pld->tripid, engineRunning);
	}

	double speed;
	const char* speedSource = NULL;
	if (getNumericPID(pld, PID_SPEED, &speed)) {
		speedSource = "obd";
	} else if (getNumericPID(pld, PID_GPS_SPEED, &speed)) {
		speedSource = "gps";
	}
	if (speedSource) {
		int moving = speed >= 1.0;
		l = appendFormat(buf, bs, l,
			"freematics_vehicle_speed_kilometres_per_hour{device_id=\"%s\",trip_id=\"%s\",source=\"%s\"} %.10g\n"
			"freematics_vehicle_moving{device_id=\"%s\",trip_id=\"%s\",source=\"%s\"} %d\n",
			pld->devid, pld->tripid, speedSource, speed,
			pld->devid, pld->tripid, speedSource, moving);
		if (getNumericPID(pld, PID_RPM, &rpm)) {
			l = appendFormat(buf, bs, l,
				"freematics_vehicle_idling{device_id=\"%s\",trip_id=\"%s\",source=\"%s\"} %d\n",
				pld->devid, pld->tripid, speedSource, engineRunning && !moving);
		}
	}

	/* PID 0x5E is the ECU fuel rate. The MAF fallback assumes petrol at 14.7:1 and 745 g/L. */
	double fuelRate;
	const char* fuelSource = NULL;
	if (getNumericPID(pld, 0x15E, &fuelRate)) {
		fuelSource = "obd_engine_fuel_rate";
	} else {
		double maf;
		if (getNumericPID(pld, PID_MAF_FLOW, &maf) && maf >= 0.0) {
			fuelRate = maf * 3600.0 / (14.7 * 745.0);
			if (isfinite(fuelRate)) fuelSource = "maf_petrol_estimate";
		}
	}
	if (fuelSource) {
		l = appendFormat(buf, bs, l,
			"freematics_vehicle_fuel_rate_litres_per_hour{device_id=\"%s\",trip_id=\"%s\",source=\"%s\"} %.10g\n",
			pld->devid, pld->tripid, fuelSource, fuelRate);
		if (speedSource && speed > 0.5 && fuelRate > 0.01) {
			double mpg = speed * 0.621371 * 4.54609 / fuelRate;
			if (!isfinite(mpg)) mpg = 0.0;
			l = appendFormat(buf, bs, l,
				"freematics_vehicle_fuel_economy_miles_per_imperial_gallon{device_id=\"%s\",trip_id=\"%s\",speed_source=\"%s\",fuel_source=\"%s\"} %.10g\n",
				pld->devid, pld->tripid, speedSource, fuelSource, mpg);
		}
	}

	/* Percent torque becomes physical torque only when the ECU also supplies its reference torque. */
	double torquePercent;
	double referenceTorque;
	if (getNumericPID(pld, 0x162, &torquePercent) && getNumericPID(pld, 0x163, &referenceTorque)
		&& getNumericPID(pld, PID_RPM, &rpm)) {
		double torque = referenceTorque * torquePercent / 100.0;
		double power = rpm > 0.0 ? torque * rpm / 9549.2965855 : 0.0;
		if (!isfinite(torque) || !isfinite(power)) return l;
		l = appendFormat(buf, bs, l,
			"freematics_vehicle_engine_torque_newton_metres{device_id=\"%s\",trip_id=\"%s\"} %.10g\n"
			"freematics_vehicle_engine_power_kilowatts{device_id=\"%s\",trip_id=\"%s\"} %.10g\n",
			pld->devid, pld->tripid, torque,
			pld->devid, pld->tripid, power);
	}

	return l;
}

static void formatDTC(uint16_t raw, char code[6], const char** system)
{
	static const char prefixes[] = "PCBU";
	static const char* systems[] = {"powertrain", "chassis", "body", "network"};
	unsigned int family = raw >> 14;
	snprintf(code, 6, "%c%X%03X", prefixes[family], (raw >> 12) & 0x3, raw & 0xFFF);
	*system = systems[family];
}

static int appendDTCMetrics(char* buf, int bs, int l, const CHANNEL_DATA* pld,
	uint16_t countPid, uint16_t basePid, const char* status, unsigned int packetAge)
{
	if (!pld || !status || countPid >= 256 * PID_MODES || basePid >= 256 * PID_MODES) return l;
	const PID_DATA* countData = pld->data + countPid;
	if (!countData->ts || l < 0 || bs <= 0 || l >= bs - 1) return l;
	double countValue;
	if (!parseFiniteNumber(countData->value, &countValue) || countValue < 0.0
		|| countValue > DTC_CODE_SLOTS || floor(countValue) != countValue) return l;
	unsigned int count = (unsigned int)countValue;
	uint64_t valueAge = packetAge;
	if (pld->deviceTick >= countData->ts) valueAge += pld->deviceTick - countData->ts;
	if (valueAge > UINT_MAX) valueAge = UINT_MAX;
	l = appendFormat(buf, bs, l,
		"freematics_diagnostic_trouble_codes{device_id=\"%s\",trip_id=\"%s\",status=\"%s\"} %u\n"
		"freematics_diagnostic_trouble_codes_age_seconds{device_id=\"%s\",trip_id=\"%s\",status=\"%s\"} %.3f\n",
		pld->devid, pld->tripid, status, count,
		pld->devid, pld->tripid, status, valueAge / 1000.0);
	for (unsigned int i = 0; i < count && basePid + i < 256 * PID_MODES; i++) {
		double rawValue;
		if (!pld->data[basePid + i].ts
			|| !parseFiniteNumber(pld->data[basePid + i].value, &rawValue)
			|| rawValue < 0.0 || rawValue > UINT16_MAX || floor(rawValue) != rawValue) continue;
		uint16_t raw = (uint16_t)rawValue;
		if (!raw) continue;
		char code[6];
		const char* system;
		formatDTC(raw, code, &system);
		l = appendFormat(buf, bs, l,
			"freematics_diagnostic_trouble_code_info{device_id=\"%s\",trip_id=\"%s\",status=\"%s\",code=\"%s\",system=\"%s\"} 1\n",
			pld->devid, pld->tripid, status, code, system);
	}
	return l;
}

int uhMetrics(UrlHandlerParam* param)
{
	if (!param || !param->pucBuffer || param->bufSize == 0) return FLAG_DATA_RAW;
	uint64_t tick = GetTickCount64();
	char* buf = param->pucBuffer;
	int bs = (int)param->bufSize;
	int l = 0;

	l = appendFormat(buf, bs, l,
		"# HELP freematics_device_connected Whether telemetry or a recent parked ping arrived within the channel timeout.\n"
		"# TYPE freematics_device_connected gauge\n"
		"# HELP freematics_device_parked Whether the device is intentionally parked and has checked in recently.\n"
		"# TYPE freematics_device_parked gauge\n"
		"# HELP freematics_device_data_age_seconds Age of the newest telemetry packet.\n"
		"# TYPE freematics_device_data_age_seconds gauge\n"
		"# HELP freematics_device_data_received_bytes_total Telemetry bytes accepted by the collector.\n"
		"# TYPE freematics_device_data_received_bytes_total counter\n"
		"# HELP freematics_device_sample_rate_per_minute Samples received per minute.\n"
		"# TYPE freematics_device_sample_rate_per_minute gauge\n"
		"# HELP freematics_device_rssi_dbm Cellular or Wi-Fi received signal strength.\n"
		"# TYPE freematics_device_rssi_dbm gauge\n"
		"# HELP freematics_network_transport Active uplink: 0 offline, 1 Wi-Fi, 2 cellular.\n"
		"# TYPE freematics_network_transport gauge\n"
		"# HELP freematics_obd_value Latest decoded value for an ECU-advertised Mode 01 PID.\n"
		"# TYPE freematics_obd_value gauge\n"
		"# HELP freematics_obd_value_age_seconds Age of the latest decoded OBD value.\n"
		"# TYPE freematics_obd_value_age_seconds gauge\n"
		"# HELP freematics_obd_protocol OBD bridge protocol number, or zero when unknown.\n"
		"# TYPE freematics_obd_protocol gauge\n"
		"# HELP freematics_obd_supported_pids Count of standard Mode 01 PIDs advertised by the ECU.\n"
		"# TYPE freematics_obd_supported_pids gauge\n"
		"# HELP freematics_obd_timeouts Cumulative OBD read failures since the active session started.\n"
		"# TYPE freematics_obd_timeouts counter\n"
		"# HELP freematics_obd_last_latency_milliseconds Slowest OBD response in the latest collection cycle.\n"
		"# TYPE freematics_obd_last_latency_milliseconds gauge\n"
		"# HELP freematics_obd_state OBD state: 0 disconnected, 1 ready, 2 degraded.\n"
		"# TYPE freematics_obd_state gauge\n"
		"# HELP freematics_obd_core_failures Consecutive failed core OBD cycles.\n"
		"# TYPE freematics_obd_core_failures gauge\n"
		"# HELP freematics_acceleration_g Vehicle acceleration by device axis.\n"
		"# TYPE freematics_acceleration_g gauge\n"
		"# HELP freematics_vehicle_info Vehicle identity reported by the ECU.\n"
		"# TYPE freematics_vehicle_info gauge\n"
		"# HELP freematics_trip_active Whether this device trip is currently receiving telemetry.\n"
		"# TYPE freematics_trip_active gauge\n"
		"# HELP freematics_trip_start_time_seconds Unix timestamp at collector login.\n"
		"# TYPE freematics_trip_start_time_seconds gauge\n"
		"# HELP freematics_trip_elapsed_seconds Collector-observed trip duration.\n"
		"# TYPE freematics_trip_elapsed_seconds gauge\n"
		"# HELP freematics_vehicle_engine_running Engine state inferred from ECU engine speed at or above 400 rpm.\n"
		"# TYPE freematics_vehicle_engine_running gauge\n"
		"# HELP freematics_vehicle_speed_kilometres_per_hour Preferred current speed from OBD, or GPS when OBD speed is absent.\n"
		"# TYPE freematics_vehicle_speed_kilometres_per_hour gauge\n"
		"# HELP freematics_vehicle_moving Whether observed vehicle speed is at or above 1 kilometre per hour.\n"
		"# TYPE freematics_vehicle_moving gauge\n"
		"# HELP freematics_vehicle_idling Engine running while observed vehicle speed is below 1 kilometre per hour.\n"
		"# TYPE freematics_vehicle_idling gauge\n"
		"# HELP freematics_vehicle_fuel_rate_litres_per_hour ECU fuel rate, or a labelled petrol MAF estimate.\n"
		"# TYPE freematics_vehicle_fuel_rate_litres_per_hour gauge\n"
		"# HELP freematics_vehicle_fuel_economy_miles_per_imperial_gallon Instantaneous UK fuel economy from observed speed and fuel rate.\n"
		"# TYPE freematics_vehicle_fuel_economy_miles_per_imperial_gallon gauge\n"
		"# HELP freematics_vehicle_engine_torque_newton_metres Physical torque derived from ECU actual and reference torque PIDs.\n"
		"# TYPE freematics_vehicle_engine_torque_newton_metres gauge\n"
		"# HELP freematics_vehicle_engine_power_kilowatts Engine power derived from ECU torque and engine speed.\n"
		"# TYPE freematics_vehicle_engine_power_kilowatts gauge\n"
		"# HELP freematics_diagnostic_trouble_codes Diagnostic trouble-code count by OBD status.\n"
		"# TYPE freematics_diagnostic_trouble_codes gauge\n"
		"# HELP freematics_diagnostic_trouble_codes_age_seconds Age of the latest diagnostic scan by status.\n"
		"# TYPE freematics_diagnostic_trouble_codes_age_seconds gauge\n"
		"# HELP freematics_diagnostic_trouble_code_info Diagnostic trouble codes reported by the ECU.\n"
		"# TYPE freematics_diagnostic_trouble_code_info gauge\n");

	for (int n = 0; n < MAX_CHANNELS && l < bs - 1; n++) {
		CHANNEL_DATA* pld = ld + n;
		if (!pld->id) continue;
		unsigned int age = tickAgeMs(tick, pld->serverDataTick);
		unsigned int pingAge = tickAgeMs(tick, pld->serverPingTick);
		int parked = (pld->flags & FLAG_SLEEPING) && pingAge <= CHANNEL_TIMEOUT * 1000U;
		int running = (pld->flags & FLAG_RUNNING) && age <= CHANNEL_TIMEOUT * 1000U;
		int connected = running || parked;
		l = appendFormat(buf, bs, l,
			"freematics_device_connected{device_id=\"%s\"} %u\n"
			"freematics_device_parked{device_id=\"%s\"} %u\n"
			"freematics_device_data_age_seconds{device_id=\"%s\"} %.3f\n"
			"freematics_device_data_received_bytes_total{device_id=\"%s\"} %u\n"
			"freematics_device_sample_rate_per_minute{device_id=\"%s\"} %.3f\n"
			"freematics_device_rssi_dbm{device_id=\"%s\"} %d\n",
			pld->devid, connected,
			pld->devid, parked,
			pld->devid, age / 1000.0,
			pld->devid, pld->dataReceived,
			pld->devid, pld->sampleRate,
			pld->devid, (int)pld->rssi);

		if (pld->vin[0]) {
			l = appendFormat(buf, bs, l,
				"freematics_vehicle_info{device_id=\"%s\",vin=\"%s\"} 1\n",
				pld->devid, pld->vin);
		}
		if (pld->tripid[0]) {
			l = appendFormat(buf, bs, l,
				"freematics_trip_active{device_id=\"%s\",trip_id=\"%s\"} %u\n"
				"freematics_trip_start_time_seconds{device_id=\"%s\",trip_id=\"%s\"} %llu\n"
				"freematics_trip_elapsed_seconds{device_id=\"%s\",trip_id=\"%s\"} %u\n",
				pld->devid, pld->tripid, running ? 1 : 0,
				pld->devid, pld->tripid, (unsigned long long)pld->sessionStartTime,
				pld->devid, pld->tripid, pld->elapsedTime);
		}

		l = appendScalarMetric(buf, bs, l, "freematics_device_temperature_celsius", pld->devid, pld->tripid, pld->data + PID_DEVICE_TEMP, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_network_transport", pld->devid, pld->tripid, pld->data + PID_NETWORK_TRANSPORT, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_obd_protocol", pld->devid, pld->tripid, pld->data + PID_OBD_PROTOCOL, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_obd_supported_pids", pld->devid, pld->tripid, pld->data + PID_OBD_SUPPORTED_PIDS, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_obd_timeouts", pld->devid, pld->tripid, pld->data + PID_OBD_TIMEOUTS, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_obd_last_latency_milliseconds", pld->devid, pld->tripid, pld->data + PID_OBD_LAST_LATENCY, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_obd_state", pld->devid, pld->tripid, pld->data + PID_OBD_STATE, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_obd_core_failures", pld->devid, pld->tripid, pld->data + PID_OBD_FAST_FAILURES, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_device_battery_voltage_volts", pld->devid, pld->tripid, pld->data + PID_BATTERY_VOLTAGE, 0.01);
		l = appendScalarMetric(buf, bs, l, "freematics_gps_latitude_degrees", pld->devid, pld->tripid, pld->data + PID_GPS_LATITUDE, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_gps_longitude_degrees", pld->devid, pld->tripid, pld->data + PID_GPS_LONGITUDE, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_gps_altitude_metres", pld->devid, pld->tripid, pld->data + PID_GPS_ALTITUDE, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_gps_speed_kilometres_per_hour", pld->devid, pld->tripid, pld->data + PID_GPS_SPEED, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_gps_heading_degrees", pld->devid, pld->tripid, pld->data + PID_GPS_HEADING, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_gps_satellites", pld->devid, pld->tripid, pld->data + PID_GPS_SAT_COUNT, 1);
		l = appendScalarMetric(buf, bs, l, "freematics_gps_hdop", pld->devid, pld->tripid, pld->data + PID_GPS_HDOP, 0.1);
		l = appendScalarMetric(buf, bs, l, "freematics_trip_distance_kilometres", pld->devid, pld->tripid, pld->data + PID_TRIP_DISTANCE, 1);

		if (pld->data[PID_ACC].ts) {
			double x, y, z;
			if (sscanf(pld->data[PID_ACC].value, "%lf;%lf;%lf", &x, &y, &z) == 3
				&& isfinite(x) && isfinite(y) && isfinite(z)) {
				l = appendFormat(buf, bs, l,
					"freematics_acceleration_g{device_id=\"%s\",trip_id=\"%s\",axis=\"x\"} %.10g\n"
					"freematics_acceleration_g{device_id=\"%s\",trip_id=\"%s\",axis=\"y\"} %.10g\n"
					"freematics_acceleration_g{device_id=\"%s\",trip_id=\"%s\",axis=\"z\"} %.10g\n",
					pld->devid, pld->tripid, x, pld->devid, pld->tripid, y, pld->devid, pld->tripid, z);
			}
		}
		if (pld->data[0x25].ts) {
			double yaw, pitch, roll;
			if (sscanf(pld->data[0x25].value, "%lf;%lf;%lf", &yaw, &pitch, &roll) == 3
				&& isfinite(yaw) && isfinite(pitch) && isfinite(roll)) {
				l = appendFormat(buf, bs, l,
					"freematics_orientation_degrees{device_id=\"%s\",trip_id=\"%s\",axis=\"yaw\"} %.10g\n"
					"freematics_orientation_degrees{device_id=\"%s\",trip_id=\"%s\",axis=\"pitch\"} %.10g\n"
					"freematics_orientation_degrees{device_id=\"%s\",trip_id=\"%s\",axis=\"roll\"} %.10g\n",
					pld->devid, pld->tripid, yaw, pld->devid, pld->tripid, pitch, pld->devid, pld->tripid, roll);
			}
		}

		for (uint16_t pid = 0x100; pid < 0x200 && l < bs - 1; pid++) {
			PID_DATA* value = pld->data + pid;
			if (!value->ts) continue;
			const OBD_PID_META* meta = findOBDPIDMeta(pid);
			if (!meta) continue;
			double number;
			if (!parseFiniteNumber(value->value, &number)) continue;
			unsigned int valueAge = age;
			if (pld->deviceTick >= value->ts) valueAge += pld->deviceTick - value->ts;
			l = appendFormat(buf, bs, l,
				"freematics_obd_value{device_id=\"%s\",trip_id=\"%s\",pid=\"0x%03X\",name=\"%s\",description=\"%s\",unit=\"%s\"} %.10g\n"
				"freematics_obd_value_age_seconds{device_id=\"%s\",trip_id=\"%s\",pid=\"0x%03X\",name=\"%s\"} %.3f\n",
				pld->devid, pld->tripid, pid, meta->name, meta->description, meta->unit, number,
				pld->devid, pld->tripid, pid, meta->name, valueAge / 1000.0);
		}

		l = appendDerivedVehicleMetrics(buf, bs, l, pld);
		l = appendDTCMetrics(buf, bs, l, pld, PID_DTC_STORED_COUNT, PID_DTC_STORED_BASE, "stored", age);
		l = appendDTCMetrics(buf, bs, l, pld, PID_DTC_PENDING_COUNT, PID_DTC_PENDING_BASE, "pending", age);
		l = appendDTCMetrics(buf, bs, l, pld, PID_DTC_PERMANENT_COUNT, PID_DTC_PERMANENT_BASE, "permanent", age);
	}

	param->contentLength = (unsigned int)(l < 0 ? 0 : l);
	param->contentType = HTTPFILETYPE_TEXT;
	return FLAG_DATA_RAW;
}

uint8_t hex2uint8(const char *p)
{
	uint8_t c1 = *p;
	uint8_t c2 = *(p + 1);
	if (c1 >= 'A' && c1 <= 'F')
		c1 -= 7;
	else if (c1 >= 'a' && c1 <= 'f')
		c1 -= 39;
	else if (c1 < '0' || c1 > '9')
		return 0;

	if (c2 == 0)
		return (c1 & 0xf);
	else if (c2 >= 'A' && c2 <= 'F')
		c2 -= 7;
	else if (c2 >= 'a' && c2 <= 'f')
		c2 -= 39;
	else if (c2 < '0' || c2 > '9')
		return 0;

	return c1 << 4 | (c2 & 0xf);
}

int hex2uint16(const char *p)
{
	char c = *p;
	uint16_t i = 0;
	char n;
	for (n = 0; c && n < 4; c = *(++p)) {
		if (c >= 'A' && c <= 'F') {
			c -= 7;
		}
		else if (c >= 'a' && c <= 'f') {
			c -= 39;
		}
		else if (c == ' ') {
			continue;
		}
		else if (c == '#' || c == '=' || c == ',' || c == ';' || c == ':') {
			return i;
		}
		else if (c < '0' || c > '9') {
			return -1;
		}
		i = (i << 4) | (c & 0xF);
		n++;
	}
	return i;
}

int ishex(char c)
{
	return (c >= '0' && c <= '9') || (c >= 'A' && c <= 'F') || (c >= 'a' && c <= 'f');
}

int isnum(const char* s)
{
	do {
		if (!isdigit(*s)) return FALSE;;
	} while (*(++s));
	return TRUE;
}

int checkVIN(const char* vin)
{
	if (!vin || strlen(vin) != 17) return 0;
	int n = 0;
	for (const char *p = vin; *p; p++, n++) {
		if (!((*p >= 'A' && *p <= 'Z') || (*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9') || *p == '-' || *p == '_' || (*p == ' ' && *(p + 1) == ' '))) {
			return 0;
		}
	}
	return n >= 8;
}

CHANNEL_DATA* findChannelByID(uint32_t id)
{
	if (id) {
		int i;
		for (i = 0; i < MAX_CHANNELS; i++) {
			if (ld[i].id == id) {
				//printf("Channel found (ID:%u)\n", id);
				return ld + i;
			}
		}
	}
	printf("Channel not found (ID:%u)\n", id);
	return 0;
}

CHANNEL_DATA* findChannelByDeviceID(const char* devid)
{
	if (devid && *devid) {
		int i;
		for (i = 0; i < MAX_CHANNELS; i++) {
			if (ld[i].id && !strcmp(ld[i].devid, devid)) {
				return ld + i;
			}
		}
	}
	return 0;
}

void initChannel(CHANNEL_DATA* pld, int cacheSize)
{
	if (cacheSize <= 0) cacheSize = CACHE_INIT_SIZE;
	if (cacheSize > CACHE_MAX_SIZE) cacheSize = CACHE_MAX_SIZE;
	pld->cacheSize = (uint32_t)cacheSize;
	pld->cache = calloc((size_t)cacheSize, sizeof(CACHE_DATA));
	if (!pld->cache) {
		/* Keep modulo operations safe even if the bounded allocation fails. */
		pld->cacheSize = 1;
	}
	pld->cacheReadPos = 0;
	pld->cacheWritePos = 0;
	pld->recvCount = 0;
	pld->txCount = 0;
	pld->dataReceived = 0;
	pld->proxyTick = 0;
	memset(pld->cmd, 0, sizeof(pld->cmd));
}

CHANNEL_DATA* findEmptyChannel()
{
	unsigned int id = 1;
	int index = -1;
	int i;
	for (i = 0; i < MAX_CHANNELS; i++) {
		if (ld[i].id >= id) {
			id = ld[i].id + 1;
		}
	}
	for (i = 0; i < MAX_CHANNELS; i++) {
		if (!ld[i].id) {
			// empty channel found
			index = i;
			break;
		}
	}
	if (index == -1) {
		return 0;
	}
	// initialize channel
	memset(&ld[index], 0, sizeof(CHANNEL_DATA));
	ld[index].id = id;
	return ld + index;
}

void removeChannel(CHANNEL_DATA* pld)
{
	if (pld->cache) free(pld->cache);
	if (pld->fp) fclose(pld->fp);
	memset(pld, 0, sizeof(CHANNEL_DATA));
}

FILE* getLogFile()
{
	static uint32_t curDate = 0;
	static FILE *fpLog = 0;
	time_t t = time(NULL);
	struct tm *btm = gmtime(&t);
	uint32_t date = (btm->tm_year + 1900) * 10000 + (btm->tm_mon + 1) * 100 + btm->tm_mday;
	if (date != curDate) {
		char path[128];
		snprintf(path, sizeof(path), "%s/%u.txt", logDir, date);
		if (fpLog && fpLog != stderr) {
			fclose(fpLog);
		}
#ifndef _DEBUG
		fpLog = fopen(path, "a+");
#endif
		if (!fpLog) fpLog = stderr;
		curDate = date;
	}
	fprintf(fpLog, "[%02u:%02u:%02u]", btm->tm_hour, btm->tm_min, btm->tm_sec);
	return fpLog;
}

FILE* createDataFile(CHANNEL_DATA* pld)
{
	if (!pld) return NULL;

	if (pld->fp) fclose(pld->fp);

	if (!IsDir(dataDir) && mkdir(dataDir, 0755) < 0) {
		char* errstr = strerror(errno);
		fprintf(getLogFile(), "Can't create data directory '%s': %s\n", dataDir, errstr);
	}

	time_t t = time(NULL);
	struct tm* btm = gmtime(&t);
	if (!btm) return NULL;
	char filename[256];
	int n = snprintf(filename, sizeof(filename), "%s/%s", dataDir, pld->devid);
	if (n < 0 || (size_t)n >= sizeof(filename)) return NULL;
	if (!IsDir(filename)) {
		fprintf(getLogFile(), "New device:%s\n", pld->devid);
		mkdir(filename, 0755);
	}

	int written = snprintf(filename + n, sizeof(filename) - (size_t)n, "/%04u", btm->tm_year + 1900);
	if (written < 0 || (size_t)written >= sizeof(filename) - (size_t)n) return NULL;
	n += written;
	mkdir(filename, 0755);
	written = snprintf(filename + n, sizeof(filename) - (size_t)n, "/%02u", btm->tm_mon + 1);
	if (written < 0 || (size_t)written >= sizeof(filename) - (size_t)n) return NULL;
	n += written;
	mkdir(filename, 0755);
	written = snprintf(filename + n, sizeof(filename) - (size_t)n, "/%02u", btm->tm_mday);
	if (written < 0 || (size_t)written >= sizeof(filename) - (size_t)n) return NULL;
	n += written;
	written = snprintf(filename + n, sizeof(filename) - (size_t)n, "/%04u%02u%02u-%02u%02u%02u.txt",
		btm->tm_year + 1900,
		btm->tm_mon + 1,
		btm->tm_mday,
		btm->tm_hour,
		btm->tm_min,
		btm->tm_sec);
	if (written < 0 || (size_t)written >= sizeof(filename) - (size_t)n) return NULL;
	if (snprintf(pld->tripid, sizeof(pld->tripid), "%04u%02u%02u-%02u%02u%02u",
		btm->tm_year + 1900, btm->tm_mon + 1, btm->tm_mday,
		btm->tm_hour, btm->tm_min, btm->tm_sec) >= (int)sizeof(pld->tripid)) return NULL;
	pld->sessionStartTime = (uint64_t)t;
	pld->fp = fopen(filename, "a+");
	if (!pld->fp) return NULL;
	if (ftell(pld->fp) == 0
		&& pld->data[PID_GPS_LATITUDE].ts && pld->data[PID_GPS_LONGITUDE].ts) {
		fprintf(pld->fp, "%X:%s,%X:%s,%X:%s\n",
			PID_GPS_LATITUDE, pld->data[PID_GPS_LATITUDE].value,
			PID_GPS_LONGITUDE, pld->data[PID_GPS_LONGITUDE].value,
			PID_GPS_ALTITUDE, pld->data[PID_GPS_ALTITUDE].value);
	}
	return pld->fp;
}

void deviceLogin(CHANNEL_DATA* pld)
{
	pld->flags |= FLAG_RUNNING;
	pld->flags &= ~FLAG_SLEEPING;
	pld->proxyTick = 0;
	// clear stats
	pld->dataReceived = 0;
	pld->recvCount = 0;
	pld->txCount = 0;
	pld->elapsedTime = 0;
	createDataFile(pld);
	SaveChannels();
	fprintf(getLogFile(), " LOGIN:%s\n", pld->devid);
}

void deviceLogout(CHANNEL_DATA* pld)
{
	uint64_t serverTick = GetTickCount64();
	pld->flags &= ~FLAG_RUNNING;
	pld->serverPingTick = serverTick;
	if (pld->fp) {
		fclose(pld->fp);
		pld->fp = 0;
	}
	fprintf(getLogFile(), " LOGOUT:%s\n", pld->devid);
}

static uint32_t payloadTimestamp(const char* payload)
{
	const char* p = payload;
	while (p && *p) {
		int pid = hex2uint16(p);
		const char* sep = p;
		while (ishex(*sep)) sep++;
		if ((*sep == ':' || *sep == '=') && pid == 0) return (uint32_t)atol(sep + 1);
		p = strchr(p, ',');
		if (p) p++;
	}
	return 0;
}

void clearLiveData(CHANNEL_DATA* pld)
{
	/* Keep the archive cache. Only the current-value snapshot belongs to a session. */
	memset(pld->data, 0, sizeof(pld->data));
	pld->deviceTick = 0;
}

int processPayload(char* payload, CHANNEL_DATA* pld, uint16_t eventID)
{
	uint64_t tick = GetTickCount64();
	uint32_t payloadTs = eventID == 0 ? payloadTimestamp(payload) : 0;
	int newTrip = payloadTs && pld->deviceTick &&
		((payloadTs < pld->deviceTick && pld->deviceTick - payloadTs > PROXY_MAX_TIME_BEHIND) ||
		 (payloadTs > pld->deviceTick && payloadTs - pld->deviceTick > SESSION_GAP));
	if (eventID == 0) {
		if (newTrip) {
			/* A device clock boundary is a trip boundary, even without LOGIN. */
			if (pld->fp) fclose(pld->fp);
			pld->fp = 0;
			clearLiveData(pld);
			pld->sessionStartTick = tick;
			pld->serverDataTick = tick;
			deviceLogin(pld);
		}
		else if (!pld->fp) {
			/* A closed archive means a real new trip; a timeout leaves fp open. */
			clearLiveData(pld);
			pld->sessionStartTick = pld->serverDataTick;
			deviceLogin(pld);
		}
		else if (!(pld->flags & FLAG_RUNNING)) {
			/* Transport timeout is liveness only, not an archive boundary. */
			pld->flags |= FLAG_RUNNING;
			pld->flags &= ~(FLAG_SLEEPING | FLAG_PINGED);
		}
		// save data to log file
		if (pld->fp) {
			fprintf(pld->fp, "%s\n", payload);
		}
	}

	char *p = payload;
	uint32_t ts = 0;
	int count = 0;
	do {
		int pid = hex2uint16(p);
		if (pid == -1) {
			p = strchr(p, ',');
			if (p) *(p++) = 0;
			continue;
		}
		while (ishex(*p)) p++;
		if (*p != ':' && *p != '=') break;
		char *value = ++p;
		p = strchr(p, ',');
		if (p) *(p++) = 0;
		char* checksum = strchr(value, '*');
		if (checksum) *checksum = 0;
		size_t len = strlen(value);
		if (len >= MAX_PID_DATA_LEN) len = MAX_PID_DATA_LEN - 1;
		// now we have pid and value
		if (pid == 0) {
			// special PID 0 for timestamp
			ts = atol(value);
			continue;
		}
		if (ts == 0) {
			// no valid timestamp yet
			continue;
		}
		// store in table
		int m = pid >> 8;
		if (m < PID_MODES) {
			pld->data[pid].ts = ts;
			memcpy(pld->data[pid].value, value, len + 1);
			// collect some stats
			switch (pid) {
			case PID_RSSI: /* signal strength */
				pld->rssi = atoi(value);
				break;
			case PID_DEVICE_TEMP:
				pld->deviceTemp = atoi(value);
				break;
			}
		}
		count++;
		// store in cache
		if (pld->cache && pld->cacheSize) {
			if (pld->cacheReadPos != pld->cacheWritePos && pld->cache[pld->cacheReadPos].ts > ts) {
				// clear cache as data looks staled
				pld->cacheReadPos = 0;
				pld->cacheWritePos = 0;
			}
			CACHE_DATA *d = &pld->cache[pld->cacheWritePos];
			d->ts = ts;
			d->pid = pid;
			d->len = (uint8_t)len;
			memcpy(d->data, value, len);
			d->data[len] = 0;
			// adjust cache pointers
			pld->cacheWritePos = (pld->cacheWritePos + 1) % pld->cacheSize;
			if (pld->cacheWritePos == pld->cacheReadPos) {
				// if write pos catch up with read pos (one lap ahead)
				// move forward read pos to discard just overwrited data
				pld->cacheReadPos = (pld->cacheReadPos + 1) % pld->cacheSize;
			}
		}
	} while (p && *p);
	if (ts == 0) ts = pld->deviceTick;
	int64_t interval = (int64_t)ts - (int64_t)pld->deviceTick;
	if (ts) pld->deviceTick = ts;

	if (pld->flags & FLAG_RUNNING) {
		// normal
		if (interval > 100) pld->sampleRate = (float)count * 60000 / interval;
		pld->elapsedTime = (uint32_t)((tick - pld->sessionStartTick) / 1000);
		pld->flags &= ~FLAG_SLEEPING;
	}
	else if (eventID == 0) {
		/* Normal recovery above preserves the open archive file. */
		pld->flags |= FLAG_RUNNING;
		pld->elapsedTime = (uint32_t)((tick - pld->sessionStartTick) / 1000);
	}
	if (!(pld->flags & FLAG_SLEEPING)) {
		pld->serverDataTick = tick;
	}

	pld->recvCount++;

	printf("[%u] #%u %u bytes | Samples:%u | Device Tick:%u\n", pld->id, pld->recvCount, pld->dataReceived, count, pld->deviceTick);
	return count;
}

static inline void setPIDData(CHANNEL_DATA* pld, int pid, uint32_t ts, const char* value)
{
	if (!pld || pid < 0 || pid >= 256 * PID_MODES || !value) return;
	pld->data[pid].ts = ts;
	strncpy(pld->data[pid].value, value, MAX_PID_DATA_LEN - 1);
	pld->data[pid].value[MAX_PID_DATA_LEN - 1] = 0;
}

void SaveChannels()
{
	char path[256];
	snprintf(path, sizeof(path), "%s/channels.dat", dataDir);
	FILE *fp = fopen(path, "wb");
	if (!fp) return;
	printf("Saving channels");
	for (int i = 0; i < MAX_CHANNELS; i++) {
		if (ld[i].id) printf(" [%d]", ld[i].id);
	}
	printf("...");
	fwrite(ld, MAX_CHANNELS, sizeof(CHANNEL_DATA), fp);
	fclose(fp);
	printf("OK\n");
}

int LoadChannels()
{
	char path[256];
	snprintf(path, sizeof(path), "%s/channels.dat", dataDir);
	FILE *fp = fopen(path, "rb");
	if (!fp) return 0;

	fseek(fp, 0, SEEK_END);
	unsigned int len = ftell(fp);
	fseek(fp, 0, SEEK_SET);
	if (len == MAX_CHANNELS * sizeof(CHANNEL_DATA)) {
		fread(ld, MAX_CHANNELS, sizeof(CHANNEL_DATA), fp);
	}
	else {
		fprintf(stderr, "Channel data file size mismatch (expected %u, actual %u)\n", (unsigned int)(MAX_CHANNELS * sizeof(CHANNEL_DATA)), len);
	}
	fclose(fp);
	int count = 0;
	for (int i = 0; i < MAX_CHANNELS; i++) {
		int valid = 1;
		ld[i].devid[sizeof(ld[i].devid) - 1] = 0;
		ld[i].tripid[sizeof(ld[i].tripid) - 1] = 0;
		ld[i].vin[sizeof(ld[i].vin) - 1] = 0;
		for (char* p = ld[i].devid; *p; p++) if (!isalnum((unsigned char)*p)) valid = 0;
		if (ld[i].id && valid && strlen(ld[i].devid) >= 4) {
			printf("[%u] ID:%u DEVID:%s\n", i, ld[i].id, ld[i].devid);
			/* Persisted pointers, liveness flags and ticks are process-local. */
			ld[i].cache = 0;
			ld[i].fp = 0; /* file handle no longer valid*/
			ld[i].flags = 0;
			ld[i].serverDataTick = 0;
			ld[i].serverPingTick = 0;
			ld[i].serverSyncTick = 0;
			ld[i].sessionStartTick = 0;
			ld[i].deviceTick = 0;
			ld[i].proxyTick = 0;
			ld[i].cmdCount = 0;
			ld[i].ip.laddr = 0;
			memset(ld[i].data, 0, sizeof(ld[i].data));
			initChannel(&ld[i], (int)ld[i].cacheSize);
			count++;
		}
		else {
			memset(ld + i, 0, sizeof(CHANNEL_DATA));
		}
	}
	printf("%d channels loaded\n", count);
	return count;
}

static unsigned int tickAgeMs(uint64_t now, uint64_t then)
{
	if (!then || now < then) return UINT_MAX;
	uint64_t age = now - then;
	return age > UINT_MAX ? UINT_MAX : (unsigned int)age;
}

void CheckChannels()
{
	uint64_t tick = GetTickCount64();
	for (int i = 0; i < MAX_CHANNELS; i++) {
		if (!ld[i].id) continue;
		CHANNEL_DATA* pld = ld + i;
		if ((pld->flags & FLAG_RUNNING) && tickAgeMs(tick, pld->serverDataTick) > CHANNEL_TIMEOUT * 1000U) {
			pld->flags &= ~FLAG_RUNNING;
		}
		if ((pld->flags & FLAG_SLEEPING) && tickAgeMs(tick, pld->serverPingTick) > CHANNEL_TIMEOUT * 1000U) {
			pld->flags &= ~(FLAG_SLEEPING | FLAG_PINGED);
		}
	}
}

void showLiveData(CHANNEL_DATA* pld)
{
	int i = 0;
	printf("[DEVID]%s\n", pld->devid);
	printf("[OBD]");
	for (i = 0x100; i < 0x100 * PID_MODES; i++) {
		if (pld->data[i].ts) {
			printf("%4X=%s ", i, pld->data[i].value);
		}
	}
	printf("\n");
	if (pld->data[PID_GPS_TIME].ts) {
		printf("[GPS]UTC:%s LAT:%s LNG:%s ALT:%sm Speed:%skm/h Sat:%s\n",
			pld->data[PID_GPS_TIME].value, pld->data[PID_GPS_LATITUDE].value, pld->data[PID_GPS_LONGITUDE].value,
			pld->data[PID_GPS_ALTITUDE].value, pld->data[PID_GPS_SPEED].value, pld->data[PID_GPS_SAT_COUNT].value);
	}
	printf("\n");
}

static int isJSONNumberRange(const char* start, const char* end)
{
	const char* p = start;
	if (p == end) return 0;
	if (*p == '-') p++;
	if (p == end) return 0;
	if (*p == '0') {
		p++;
		if (p != end && isdigit((unsigned char)*p)) return 0;
	}
	else {
		if (p == end || *p < '1' || *p > '9') return 0;
		while (p != end && isdigit((unsigned char)*p)) p++;
	}
	if (p != end && *p == '.') {
		p++;
		const char* fraction = p;
		while (p != end && isdigit((unsigned char)*p)) p++;
		if (p == fraction) return 0;
	}
	if (p != end && (*p == 'e' || *p == 'E')) {
		p++;
		if (p != end && (*p == '+' || *p == '-')) p++;
		const char* exponent = p;
		while (p != end && isdigit((unsigned char)*p)) p++;
		if (p == exponent) return 0;
	}
	return p == end;
}

static int isJSONNumber(const char* text)
{
	return text && isJSONNumberRange(text, text + strlen(text));
}

static int copyData(char* d, int bs, const char* s)
{
	if (!d || bs <= 0) return 0;
	if (!s) s = "";
	int used = 0;
	const int cap = bs - 1;
	double number;
	if (parseFiniteNumber(s, &number) && isJSONNumber(s)) {
		size_t len = strlen(s);
		if (len <= (size_t)cap) {
			memcpy(d, s, len);
			used = (int)len;
		}
		else if (cap >= 4) {
			memcpy(d, "null", 4);
			used = 4;
		}
	}
	else {
		/* A semicolon-delimited list is an array only when every item is finite. */
		int isArray = strchr(s, ';') != NULL;
		int validArray = isArray;
		if (isArray) {
			const char* item = s;
			size_t required = 2;
			for (;;) {
				const char* sep = strchr(item, ';');
				const char* end = sep ? sep : item + strlen(item);
				if (end == item) validArray = 0;
				else {
					errno = 0;
					char* parsedEnd = NULL;
					double parsed = strtod(item, &parsedEnd);
					if (parsedEnd != end || errno == ERANGE || !isfinite(parsed) || !isJSONNumberRange(item, end)) validArray = 0;
				}
				if (required > SIZE_MAX - (size_t)(end - item)) validArray = 0;
				else required += (size_t)(end - item);
				if (!sep) break;
				if (required == SIZE_MAX) validArray = 0;
				else required++;
				item = sep + 1;
			}
			if (validArray && required <= (size_t)cap) {
				used = 0;
				d[used++] = '[';
				for (const char* p = s; *p; p++) d[used++] = *p == ';' ? ',' : *p;
				d[used++] = ']';
			}
			else if (cap >= 2) {
				d[0] = '[';
				d[1] = ']';
				used = 2;
			}
		}
		if (!isArray || !validArray) {
			used = 0;
			if (cap >= 2) {
				d[used++] = '"';
				for (const unsigned char* p = (const unsigned char*)s; *p && used < cap; p++) {
					char escaped[7];
					int escapedLen = 1;
					switch (*p) {
					case '\\': case '"': escaped[0] = '\\'; escaped[1] = (char)*p; escapedLen = 2; break;
					case '\b': escaped[0] = '\\'; escaped[1] = 'b'; escapedLen = 2; break;
					case '\f': escaped[0] = '\\'; escaped[1] = 'f'; escapedLen = 2; break;
					case '\n': escaped[0] = '\\'; escaped[1] = 'n'; escapedLen = 2; break;
					case '\r': escaped[0] = '\\'; escaped[1] = 'r'; escapedLen = 2; break;
					case '\t': escaped[0] = '\\'; escaped[1] = 't'; escapedLen = 2; break;
					default:
						if (*p < 0x20) escapedLen = snprintf(escaped, sizeof(escaped), "\\u%04x", *p);
						else escaped[0] = (char)*p;
						break;
					}
					if (used + escapedLen >= cap) break;
					memcpy(d + used, escaped, (size_t)escapedLen);
					used += escapedLen;
				}
				d[used++] = '"';
			}
			else if (cap >= 4) {
				memcpy(d, "null", 4);
				used = 4;
			}
		}
	}
	d[used] = 0;
	return used;
}
CHANNEL_DATA* locateChannel(UrlHandlerParam* param)
{
	const char* sid;
	if (param->pucRequest[0] == '/') {
		sid = param->pucRequest + 1;
	}
	else {
		sid = mwGetVarValue(param->pxVars, "id", "");
	}

	CHANNEL_DATA *pld = 0;
	if (*sid) {
		pld = findChannelByDeviceID(sid);
	}
	return pld;
}

int uhChannelsXML(UrlHandlerParam* param)
{
	int extend = mwGetVarValueInt(param->pxVars, "extend", 0);
	uint64_t tick = GetTickCount64();
	/*
	fprintf(getLogFile(), "%u.%u.%u.%u request channels XML\n",
		param->hs->ipAddr.caddr[3], param->hs->ipAddr.caddr[2], param->hs->ipAddr.caddr[1], param->hs->ipAddr.caddr[0]);
	*/

	char *p = param->pucBuffer;
	p += sprintf(p, "<?xml version=\"1.0\" encoding=\"utf-8\"?><channels>\n");
	for (int n = 0; n < MAX_CHANNELS; n++) {
		CHANNEL_DATA* pld = ld + n;
		if (pld->id) {
			p += sprintf(p, "<channel id=\"%u\" devid=\"%s\" recv=\"%u\" rate=\"%u\" tick=\"%u\" elapsed=\"%u\" age=\"%u\" parked=\"%u\" rssi=\"%d\" flags=\"%u\"",
				pld->id, pld->devid, pld->dataReceived, (unsigned int)pld->sampleRate, pld->deviceTick, pld->elapsedTime, 
				(int)(tick - pld->serverDataTick), (pld->flags & FLAG_RUNNING) ? 0 : 1, (int)pld->rssi, pld->devflags);

			if (extend) {
				if (*pld->vin) p += sprintf(p, "<vin>%s</vin>", pld->vin);
				p += sprintf(p, "><cache size=\"%u\" read=\"%u\" write=\"%u\"/></channel>\n",
					pld->cacheSize, pld->cacheReadPos, pld->cacheWritePos);
				if (pld->ip.laddr) {
					p += sprintf(p, "<ip>%u.%u.%u.%u</ip>", pld->ip.caddr[3], pld->ip.caddr[2], pld->ip.caddr[1], pld->ip.caddr[0]);
				}
				else {
					p += sprintf(p, "<ip>%s</ip>", inet_ntoa(pld->udpPeer.sin_addr));
				}
			}
			else {
				p += sprintf(p, "/>\n");
			}

		}
		else {
			p += sprintf(p, "<channel/>\n");
		}
	}
	p += sprintf(p, "</channels>");
	param->contentLength = (int)(p - param->pucBuffer);
	param->contentType = HTTPFILETYPE_XML;
	return FLAG_DATA_RAW;
}

int uhChannels(UrlHandlerParam* param)
{
	uint64_t tick = GetTickCount64();
	int bs = param->bufSize;
	char* buf = param->pucBuffer;
	int l = 0;
	int n = 0;
	const char *cmd = mwGetVarValue(param->pxVars, "cmd", 0);
	int data = mwGetVarValueInt(param->pxVars, "data", 0);
	int extend = mwGetVarValueInt(param->pxVars, "extend", 0);
	int id = mwGetVarValueInt(param->pxVars, "id", 0);
	unsigned int refresh = mwGetVarValueInt(param->pxVars, "refresh", MAX_CHANNEL_AGE);
	const char *devid = mwGetVarValue(param->pxVars, "devid", 0);

	const char *req = param->pucRequest;
	if (!strncmp(req, "/data", 5)) {
		data = 1;
		req += 5;
	}
	if (req[0] == '/') {
		devid = req + 1;
	}
	/*
	fprintf(getLogFile(), "%u.%u.%u.%u request channels\n",
		param->hs->ipAddr.caddr[3], param->hs->ipAddr.caddr[2], param->hs->ipAddr.caddr[1], param->hs->ipAddr.caddr[0]);
	*/

	if (cmd && !strcmp(cmd, "clear")) {
		CHANNEL_DATA *pld = findChannelByID(id);
		if (pld) {
			fprintf(getLogFile(), "%u.%u.%u.%u [%u] remove channel\n",
				param->hs->ipAddr.caddr[3], param->hs->ipAddr.caddr[2], param->hs->ipAddr.caddr[1], param->hs->ipAddr.caddr[0], id);
			removeChannel(pld);
			id = 0;
		}
	}
	if (!devid) {
		l += snprintf(buf + l, bs - l, "{\"channels\":[");
	}
	for (n = 0; n < MAX_CHANNELS; n++) {
		CHANNEL_DATA* pld = ld + n;
		if (!pld->id) continue;
		if (devid && strcmp(pld->devid, devid)) continue;
		if (id == 0 || pld->id == id) {
			unsigned int age = (unsigned int)(tick - pld->serverDataTick);
			unsigned int pingage = (unsigned int)(tick - pld->serverPingTick);
			if (refresh && (age > refresh && pingage > refresh)) {
				removeChannel(pld);
				continue;
			}
			l += snprintf(buf + l, bs - l, "\n{\"id\":\"%u\",\"devid\":\"%s\",\"recv\":%u,\"rate\":%u,\"tick\":%llu,\"devtick\":%u,\"elapsed\":%u,\"age\":{\"data\":%u,\"ping\":%u},\"rssi\":%d,\"flags\":%u,\"parked\":%u",
				pld->id, pld->devid, pld->dataReceived, (unsigned int)pld->sampleRate, (unsigned long long)pld->serverDataTick, pld->deviceTick, pld->elapsedTime,
				age, pingage, (int)pld->rssi, pld->devflags, (pld->flags & FLAG_RUNNING) ? 0 : 1);

			if (extend) {
				if (*pld->vin) {
					l += snprintf(buf + l, bs - l, ",\"vin\":\"%s\"", pld->vin);
				}
				if (pld->ip.laddr) {
					l += snprintf(buf + l, bs - l, ",\"ip\":\"%u.%u.%u.%u\"", pld->ip.caddr[3], pld->ip.caddr[2], pld->ip.caddr[1], pld->ip.caddr[0]);
				}
				else {
					l += snprintf(buf + l, bs - l, ",\"ip\":\"%s\"", inet_ntoa(pld->udpPeer.sin_addr));
				}
			}

			if (data) {
				l += snprintf(buf + l, bs - l, ",\"data\":[");
				for (unsigned int i = 0; i < 0x100 * PID_MODES; i++) {
					if (pld->data[i].ts) {
						l += snprintf(buf + l, bs - l, "[%u,", i);
						if (l < bs) l += copyData(buf + l, bs - l, pld->data[i].value);
						l += snprintf(buf + l, bs - l, ",%u],", age + (pld->deviceTick - pld->data[i].ts));
					}
				}
				if (buf[l - 1] == ',') l--;
				l += snprintf(buf + l, bs - l, "]");
			}
			l += snprintf(buf + l, bs - l, "},");
		}
	}

	if (l == 0) {
		l += snprintf(buf + l, bs - l, "{}");
	}
	else if (buf[l - 1] == ',') {
		buf[--l] = 0;
	}
	if (!devid) {
		l += snprintf(buf + l, bs - l, "]}");
	}
	param->contentLength = l;
	param->contentType = HTTPFILETYPE_JSON;
	return FLAG_DATA_RAW;
}

char* findNextToken(char* s)
{
	while (*s && (isdigit(*s) || *s == '-' || *s == '.' || *s == ',' || *s == '/' || *s == ';')) s++;
	return s + 1;
}

CHANNEL_DATA* assignChannel(const char* devid)
{
	if (!devid) {
		fprintf(getLogFile(), "Invalid ID");
		return 0;
	}
	size_t length = strlen(devid);
	if (length < MIN_DEVID_LEN || length >= sizeof(((CHANNEL_DATA*)0)->devid)) {
		fprintf(getLogFile(), "Invalid ID");
		return 0;
	}
	for (const unsigned char* p = (const unsigned char*)devid; *p; p++) {
		if (!isalnum(*p)) return 0;
	}

	CHANNEL_DATA* pld = findChannelByDeviceID(devid);
	if (pld) return pld;
	pld = findEmptyChannel();
	if (!pld) return 0;
	strncpy(pld->devid, devid, sizeof(pld->devid) - 1);
	pld->devid[sizeof(pld->devid) - 1] = 0;
	initChannel(pld, CACHE_INIT_SIZE);
	pld->cacheReadPos = 0;
	pld->cacheWritePos = 0;
	memset(pld->data, 0, sizeof(pld->data));
	pld->dataReceived = 0;
	pld->elapsedTime = 0;
	pld->serverDataTick = GetTickCount64();
	SaveChannels();
	printf("DEVID:%s ID:%u\r\n", devid, pld->id);
	return pld;
}

int uhPost(UrlHandlerParam* param)
{
	param->contentLength = 0;
	CHANNEL_DATA* pld = locateChannel(param);
	if (!pld) {
		param->hs->response.statusCode = 403;
		param->contentLength = 0;
		return FLAG_DATA_RAW;
	}

	const char* lat = mwGetVarValue(param->pxVars, "lat", 0);
	const char* lon = mwGetVarValue(param->pxVars, "lon", 0);
	uint32_t ts = mwGetVarValueInt(param->pxVars, "timestamp", 0);
	const char* alt = mwGetVarValue(param->pxVars, "altitude", 0);
	const char* speed = mwGetVarValue(param->pxVars, "speed", 0);
	const char* heading = mwGetVarValue(param->pxVars, "heading", 0);
	if (ts) pld->deviceTick = ts;
	if (lat) setPIDData(pld, PID_GPS_LATITUDE, ts, lat);
	if (lon) setPIDData(pld, PID_GPS_LONGITUDE, ts, lon);
	if (speed) setPIDData(pld, PID_GPS_SPEED, ts, speed);
	if (alt) setPIDData(pld, PID_GPS_ALTITUDE, ts, alt);
	if (heading) setPIDData(pld, PID_GPS_HEADING, ts, heading);

	if (!param->payloadSize) {
		printf("GET from %u.%u.%u.%u | LAT:%s LON:%s ALT:%sm\n",
			param->hs->ipAddr.caddr[3], param->hs->ipAddr.caddr[2], param->hs->ipAddr.caddr[1], param->hs->ipAddr.caddr[0],
			lat, lon, alt);
		return FLAG_DATA_RAW;
	}

	printf("POST from %u.%u.%u.%u | ",
		param->hs->ipAddr.caddr[3], param->hs->ipAddr.caddr[2], param->hs->ipAddr.caddr[1], param->hs->ipAddr.caddr[0]);

	unsigned int count = processPayload(param->pucPayload, pld, 0);
	pld->dataReceived += param->payloadSize;
	pld->ip = param->hs->ipAddr;

	param->contentLength = sprintf(param->pucBuffer, "OK %u", count);
	param->contentType = HTTPFILETYPE_TEXT;
	return FLAG_DATA_RAW;
}

int uhGet(UrlHandlerParam* param)
{
	CHANNEL_DATA* pld = locateChannel(param);
	if (!pld) {
		param->hs->response.statusCode = 403;
		param->contentLength = 0;
		return FLAG_DATA_RAW;
	}

	uint64_t tick = GetTickCount64();
	int bs = param->bufSize;
	char* buf = param->pucBuffer;
	int l = 0;
	unsigned int age = pld->serverDataTick ? (unsigned int)(tick - pld->serverDataTick) : 0;
	unsigned int pingage = pld->serverPingTick ? (unsigned int)(tick - pld->serverPingTick) : 0;
	l += snprintf(buf + l, bs - l, "{\"stats\":{\"tick\":%llu,\"devtick\":%u,\"elapsed\":%u,\"age\":{\"data\":%u,\"ping\":%u},\"rssi\":%d,\"flags\":%u,\"parked\":%u}",
		(unsigned long long)pld->serverDataTick, pld->deviceTick, pld->elapsedTime,
		age, pingage, (int)pld->rssi, pld->devflags, (pld->flags & FLAG_RUNNING) ? 0 : 1);

	l += snprintf(buf + l, bs - l, ",\"data\":[");
	for (unsigned int i = 0; i < 0x100 * PID_MODES; i++) {
		if (pld->data[i].ts) {
			l += snprintf(buf + l, bs - l, "[%u,", i);
			if (l < bs) l += copyData(buf + l, bs - l, pld->data[i].value);
			l += snprintf(buf + l, bs - l, ",%u],",
				pld->deviceTick >= pld->data[i].ts ? (age + pld->deviceTick - pld->data[i].ts) : 0);
		}
	}
	if (buf[l - 1] == ',') l--;
	l += snprintf(buf + l, bs - l, "]}");

	param->contentLength = l;
	param->contentType = HTTPFILETYPE_JSON;
	return FLAG_DATA_RAW;
}

int uhPull(UrlHandlerParam* param)
{
	param->contentType = HTTPFILETYPE_JSON;
	param->contentLength = 0;
	CHANNEL_DATA *pld = locateChannel(param);
	if (!pld) {
		param->hs->response.statusCode = 403;
		param->contentLength = 0;
		return FLAG_DATA_RAW;
	}

	uint64_t startts = mwGetVarValueInt64(param->pxVars, "ts");
	uint64_t endts = mwGetVarValueInt64(param->pxVars, "endts");
	uint32_t rollback = mwGetVarValueInt(param->pxVars, "rollback", 0);
	int pid = mwGetVarValueInt(param->pxVars, "pid", 0);

	uint64_t tick = GetTickCount64();
	unsigned int age = (unsigned int)(tick - pld->serverDataTick);
	unsigned int pingage = (unsigned int)(tick - pld->serverPingTick);

	int bytes = 0;
	char* buf = param->pucBuffer;
	int bufsize = param->bufSize;

	bytes += sprintf(buf + bytes, "{");
	bytes += snprintf(buf + bytes, bufsize - bytes, "\"stats\":{\"recv\":%u,\"rate\":%u,\"tick\":%llu,\"devtick\":%u,\"elapsed\":%u,\"age\":{\"data\":%u,\"ping\":%u},\"parked\":%u}",
		pld->dataReceived, (unsigned int)pld->sampleRate, (unsigned long long)pld->serverDataTick, pld->deviceTick, pld->elapsedTime, age, pingage, (pld->flags & FLAG_RUNNING) ? 0 : 1);

	bytes += snprintf(buf + bytes, bufsize - bytes, ",\"live\":[");
	for (unsigned int i = 0; i < 0x100 * PID_MODES; i++) {
		if (pld->data[i].ts) {
			bytes += snprintf(buf + bytes, bufsize - bytes, "[%u,", i);
			if (bytes < bufsize) bytes += copyData(buf + bytes, bufsize - bytes, pld->data[i].value);
			bytes += snprintf(buf + bytes, bufsize - bytes, "],");
		}
	}
	if (buf[bytes - 1] == ',') bytes--;
	bytes += snprintf(buf + bytes, bufsize - bytes, "]");

	if (rollback) {
		// calculate and override ts
		uint64_t t = GetTickCount64() - pld->serverDataTick + pld->deviceTick;
		startts = t > rollback ? (t - rollback) : 0;
	}
	// start of data array
	bytes += sprintf(buf + bytes, ",\"data\":[");
	uint32_t readPos = pld->cacheReadPos;
	uint64_t begin = 0;
	int bytesMargin = bytes;
	uint32_t lastts = 0;
	for (; pld->cache && pld->cacheSize && readPos != pld->cacheWritePos; readPos = (readPos + 1) % pld->cacheSize) {
		CACHE_DATA *d = pld->cache + readPos;
		if (d->ts < lastts) {
			// timestamp looping or device reset detected, wipe out all previous data
			bytes = bytesMargin;
		}
		lastts = d->ts;
		if (d->ts >= startts) {
			if (endts && d->ts >= endts) break;
			if (bytes + d->len + 64 > bufsize) {
				// buffer full
				break;
			}
			if (d->data[0] && (pid == 0 || pid == d->pid)) {
				bytes += sprintf(buf + bytes, "[%u,%d,", d->ts, d->pid);
				if (bytes < bufsize) bytes += copyData(buf + bytes, bufsize - bytes, d->data);
				bytes += sprintf(buf + bytes, "],");
			}
			// keep ts range
			if (begin == 0) begin = d->ts;
		}
	}
	if (buf[bytes - 1] == ',') bytes--;
	// end of data array
	buf[bytes++] = ']';
	if (readPos == pld->cacheWritePos) {
		// cache completely read
		bytes += sprintf(buf + bytes, ",\"eos\":1");
	}
	else {
		bytes += sprintf(buf + bytes, ",\"eos\":0");
	}
	buf[bytes++] = '}';
	buf[bytes] = 0;
	param->contentLength = bytes;
	return FLAG_DATA_RAW;
}

int isNum(const char* s)
{
	while (*s) {
		if (!isdigit(*s)) return 0;
		s++;
	}
	return 1;
}

int isReadOnlyCommand(const char* cmd)
{
	static const char* const commands[] = {
		"UPTIME", "TICK", "BATT", "NET_OP", "NET_IP", "NET_PACKET",
		"NET_DATA", "NET_RATE", "RSSI", "SSID?", "APN?", "TEMP",
		"ACC", "GYRO", "GF", "VIN", "LAT", "LNG", "ALT", "SAT", "SPD", "CRS",
	};
	if (!cmd || !*cmd || strlen(cmd) >= MAX_COMMAND_MSG_LEN) return 0;
	for (unsigned int i = 0; i < sizeof(commands) / sizeof(commands[0]); i++) {
		if (!strcmp(cmd, commands[i])) return 1;
	}
	return !strncmp(cmd, "01", 2) && strlen(cmd) == 4 && ishex(cmd[2]) && ishex(cmd[3]);
}

int uhCommand(UrlHandlerParam* param)
{
	CHANNEL_DATA *pld = locateChannel(param);
	if (!pld) {
		param->hs->response.statusCode = 403;
		param->contentLength = 0;
		return FLAG_DATA_RAW;
	}

	char* cmd = mwGetVarValue(param->pxVars, "cmd", "");
	uint32_t token = mwGetVarValueInt(param->pxVars, "token", 0);
	param->contentType = HTTPFILETYPE_JSON;

	if (!*cmd && !token) {
		param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"failed\",\"error\":\"Invalid request\"}");
		return FLAG_DATA_RAW;
	}
	if (*cmd && strlen(cmd) >= MAX_COMMAND_MSG_LEN) {
		param->hs->response.statusCode = 400;
		param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"failed\",\"error\":\"Command too long\"}");
		return FLAG_DATA_RAW;
	}
	if (*cmd && !isReadOnlyCommand(cmd)) {
		param->hs->response.statusCode = 403;
		param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"failed\",\"error\":\"Command is not read-only\"}");
		return FLAG_DATA_RAW;
	}
	pld->serverDataTick = GetTickCount64();
	if (*cmd) {
		// token = 0: no token
		token = issueCommand(param->hp, pld, cmd, token);
		if (token) {
			param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"pending\",\"token\":%u}", token);
		}
		else {
			param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"failed\",\"error\":\"Command unsent\"}");
		}
	}
	else {
		COMMAND_BLOCK* cb = 0;
		int i;
		for (i = 0; i < MAX_PENDING_COMMANDS; i++) {
			if (pld->cmd[i].token == token) {
				cb = pld->cmd + i;
				break;
			}
		}
		if (!cb) {
			param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"failed\",\"error\":\"Invalid token\"}");
		}
		else if (!(cb->flags & CMD_FLAG_RESPONDED)) {
			param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"pending\",\"elapsed\":%u}", (unsigned int)(cb->tick - pld->serverDataTick));
		}
		else {
			param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"done\",\"idx\":%u,\"elapsed\":%u,\"data\":\"%s\"}",
				i, (unsigned int)cb->elapsed, cb->message);
			cb->flags |= CMD_FLAG_CHECKED;
		}
	}
	return FLAG_DATA_RAW;
}

int uhNotify(UrlHandlerParam* param)
{
	char* vin = mwGetVarValue(param->pxVars, "VIN", 0);
	int event = mwGetVarValueInt(param->pxVars, "EV", 0);
	unsigned int devflags = mwGetVarValueInt(param->pxVars, "DF", 0);
	int rssi = mwGetVarValueInt(param->pxVars, "SSI", 0);
	const char* sid;
	if (param->pucRequest[0] == '/') {
		sid = param->pucRequest + 1;
	}
	else {
		sid = mwGetVarValue(param->pxVars, "id", "");
	}
	if (!sid || strlen(sid) < 4) {
		param->hs->response.statusCode = 403;
		param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"failed\",\"error\":\"Invalid ID\"}");
		return FLAG_DATA_RAW;
	}
	param->contentType = HTTPFILETYPE_JSON;
	param->contentLength = 0;
	CHANNEL_DATA* pld = locateChannel(param);
	if (!pld && event != EVENT_LOGIN) {
		param->hs->response.statusCode = 403;
		param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"failed\",\"error\":\"Invalid channel\"}");
		return FLAG_DATA_RAW;
	}

	uint64_t tick = GetTickCount64();
	if (event == EVENT_LOGIN) {
		if (!pld) {
			pld = assignChannel(sid);
			if (!pld) {
				param->hs->response.statusCode = 403;
				return FLAG_DATA_RAW;
			}
		}

		if (checkVIN(vin)) {
			strncpy(pld->vin, vin, sizeof(pld->vin) - 1);
		}
		pld->devflags = devflags;
		pld->rssi = rssi;
		pld->ip = param->hs->ipAddr;
		if (!pld->fp) {
			clearLiveData(pld);
			pld->sessionStartTick = tick;
			pld->serverDataTick = tick;
			deviceLogin(pld);
		}
		else {
			/* Re-login restores transport liveness without rotating an open trip. */
			pld->flags |= FLAG_RUNNING;
			pld->flags &= ~(FLAG_SLEEPING | FLAG_PINGED);
			pld->proxyTick = 0;
			pld->serverDataTick = tick;
		}
		param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"id\":%u,\"result\":\"done\"}", pld->id);
		return FLAG_DATA_RAW;
	} else if (event == EVENT_LOGOUT) {
		param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"done\"}");
		if (pld->fp) {
			fclose(pld->fp);
			pld->fp = 0;
		}
		pld->flags &= ~FLAG_RUNNING;
		deviceLogout(pld);
		SaveChannels();
		return FLAG_DATA_RAW;
	}
	else if (event == EVENT_PING) {
		// A parked HTTP client deliberately has no telemetry session. Keep a
		// short-lived server-side marker so dashboards and host notifications can
		// distinguish intentional standby from an unreachable device.
		pld->serverPingTick = tick;
		pld->flags |= FLAG_SLEEPING | FLAG_PINGED;
		pld->flags &= ~FLAG_RUNNING;
		if (rssi) pld->rssi = rssi;
		if (devflags) pld->devflags = devflags;
		SaveChannels();
		param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"done\"}");
		return FLAG_DATA_RAW;
	}
	else if (event == EVENT_SYNC) {
		param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"done\"}");
		return FLAG_DATA_RAW;
	}

	param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":\"failed\",\"error\":\"Invalid request\"}");
	param->hs->response.statusCode = 400;
	return FLAG_DATA_RAW;
}

int uhPush(UrlHandlerParam* param)
{
	//mwParseQueryString(param);
	CHANNEL_DATA *pld = locateChannel(param);
	if (!pld) {
		param->hs->response.statusCode = 403;
		param->contentLength = 0;
		return FLAG_DATA_RAW;
	}

	int n;
	char *s;
	uint64_t tick = GetTickCount64();
	pld->deviceTick = mwGetVarValueInt(param->pxVars, "ts", 0);
	int count = 0;
	for (n = 0; n < param->iVarCount; n++) {
		s = param->pxVars[n].name;
		if (isNum(s)) {
			int pid = hex2uint16(s);
			int mode = pid >> 8;
			if (pid >= 0 && mode < PID_MODES) {
				setPIDData(pld, pid, pld->deviceTick, param->pxVars[n].value);
				count++;
			}
		}
	}
	pld->serverDataTick = tick;
	pld->elapsedTime = (uint32_t)((pld->serverDataTick - pld->sessionStartTick) / 1000);
	pld->recvCount++;
	showLiveData(pld);
	param->contentType = HTTPFILETYPE_JSON;
	param->contentLength = snprintf(param->pucBuffer, param->bufSize, "{\"result\":%u}", count);
	return FLAG_DATA_RAW;
}

int uhTest(UrlHandlerParam* param)
{
	char content[64];
	time_t t = time(NULL);
	struct tm* btm = gmtime(&t);
	int len = snprintf(content, sizeof(content), "{\"date\":%02u%02u%02u,\"time\":%u%02u%02u,\"tick\":%u}",
		btm->tm_year - 100, btm->tm_mon + 1, btm->tm_mday, btm->tm_hour, btm->tm_min, btm->tm_sec, GetTickCount());
	param->contentLength = snprintf(param->pucBuffer, param->bufSize, "HTTP/1.1 200 OK\r\nServer:TeleServer\r\nContent-Length:%d\r\n\r\n%s",
		len, content);
	param->contentType = HTTPFILETYPE_TEXT;
	return FLAG_DATA_RAW | FLAG_CUSTOM_HEADER;
}

char* GetLocalAddrString()
{
	// get local ip address
	struct sockaddr_in sock;
	char hostname[256] = { 0 };
	struct hostent * lpHost;
	gethostname(hostname, sizeof(hostname));
	lpHost = gethostbyname(hostname);
	if (!lpHost) return "127.0.0.1";
	memcpy(&(sock.sin_addr), (void*)lpHost->h_addr_list[0], lpHost->h_length);
	return inet_ntoa(sock.sin_addr);
}

int ServerQuit(int arg) {
	static int quitting = 0;
	if (quitting) return 0;
	quitting = 1;
	if (arg) printf("\nCaught signal (%d). Shutting down...\n",arg);
	mwServerShutdown(&httpParam);
	SaveChannels();
	return 0;
}

void GetFullPath(char* buffer, char* argv0, char* path)
{
	char* p = strrchr(argv0, '/');
	if (!p) p = strrchr(argv0, '\\');
	if (!p) {
		strcpy(buffer, path);
	} else {
		size_t l = p - argv0 + 1;
		memcpy(buffer, argv0, l);
		strcpy(buffer + l, path);
	}
}

int genHttpPostPayload(CHANNEL_DATA* pld);

int main(int argc,char* argv[])
{
	fprintf(stderr, "Freematics Hub Version %s (built on %s)\n(C)2016-2020 Mediatronic Pty Ltd / Developed by Stanley Huang\nThis is free software and is distributed under GPL v3.0\n\n", REVISION, __DATE__);

#ifdef WIN32
	SetConsoleCtrlHandler( (PHANDLER_ROUTINE) ServerQuit, TRUE );
#else
	signal(SIGINT, (void *) ServerQuit);
	signal(SIGTERM, (void *) ServerQuit);
	signal(SIGPIPE, SIG_IGN);
#endif

	//fill in default settings
	char path[256];
	GetFullPath(path, argv[0], "htdocs");
	mwInitParam(&httpParam, 0, path, FLAG_DISABLE_RANGE, 0, 0);
	httpParam.maxClients = 256;
	httpParam.maxClientsPerIP = 16;
	httpParam.httpPort = 8080;
	httpParam.udpPort = 8081;
	httpParam.pxUrlHandler = urlHandlerList;
	httpParam.hlBindIP = htonl(INADDR_ANY);
	httpParam.pfnIncomingUDP = incomingUDPCallback;
	httpParam.pfnProxyData = phData;

#ifdef WIN32
	char dir[240];
	if (GetEnvironmentVariable("APPDATA", dir, sizeof(dir))) {
		strcat(dir, "/FreematicsHub");
		if (IsDir(dir)) {
			if (!IsDir(dataDir))
				snprintf(dataDir, sizeof(dataDir), "%s/Data", dir);
			if (!IsDir(logDir))
				snprintf(logDir, sizeof(logDir), "%s/Log", dir);
		}
	}
#endif

	loadConfig();

	//parsing command line arguments
	{
		int i;
		for (i=1;i<argc;i++) {
			if (argv[i][0]=='-') {
				switch (argv[i][1]) {
				case 'h':
					fprintf(stderr, "Usage: teleserver\n"
						"	-h	: display this help screen\n"
						"	-p	: specifiy http port [default 8080]\n"
						"	-u	: specifiy udp port [default 8081]\n"
						"	-l	: specify log file directory\n"
						"	-d	: specify data file directory\n"
						"	-m	: specifiy max clients [default 256]\n"
						"	-M	: specifiy max clients per IP\n"
						"	-n	: specifiy HTTP authentication user name for remote access [default: admin]\n"
						"	-w	: specifiy HTTP authentication password for remote access\n"
						"	-g	: do not launch GUI\n\n");
					fflush(stderr);
					exit(1);
					break;
				case 'p':
					if ((++i)<argc) httpParam.httpPort=atoi(argv[i]);
					break;
				case 'm':
					if ((++i)<argc) httpParam.maxClients=atoi(argv[i]);
					break;
				case 'M':
					if ((++i)<argc) httpParam.maxClientsPerIP=atoi(argv[i]);
					break;
				case 'g':
					noGUI = 1;
					break;
				case 'l':
					if ((++i)<argc) strncpy(logDir, argv[i], sizeof(logDir) - 1);
					break;
				case 'd':
					if ((++i)<argc) strncpy(dataDir, argv[i], sizeof(dataDir) - 1);
					break;
				case 'k':
					if ((++i)<argc) strncpy(serverKey, argv[i], sizeof(serverKey) - 1);
					break;
				case 'u':
					if (++i < argc) httpParam.udpPort = atoi(argv[i]);
					break;
				case 'n':
					if (++i < argc) strncpy(username, argv[i], sizeof(username) - 1);
					break;
				case 'w':
					if (++i < argc) strncpy(password, argv[i], sizeof(password) - 1);
					break;
				}
			}
		}
	}

	if (password[0]) {
		httpParam.pxAuthHandler = authHandlerList;
	}

	printf("Server Host: %s:%u\n", GetLocalAddrString(), httpParam.httpPort);
	if (httpParam.udpPort) {
		printf("UDP Port: %u\n", httpParam.udpPort);
	}
	printf("Max Channels: %u\n", MAX_CHANNELS);
	if (password[0]) {
		printf("Authentication: ON\n");
	}
	printf("\nWeb UI:\nhttp://%s:%u\n\n", GetLocalAddrString(), httpParam.httpPort);
	printf("Data Feed Simulator:\nhttp://%s:%u/simulator.html\n\n", GetLocalAddrString(), httpParam.httpPort);

	memset(ld, 0, sizeof(ld));
	LoadChannels();

	if (mwServerStart(&httpParam)) {
		printf("Error starting HTTP server on port %u\nPress ENTER to exit\n", httpParam.httpPort);
		return -1;
	}

	int ret = -1;
	SHELL_PARAM proc = { 0 };
#ifdef WIN32
	if (!noGUI) {
		proc.flags = SF_SHOW_WINDOW;
		ret = ShellExec(&proc, "electron/electron app");
	}
#endif
	if (ret == 0) {
		do {
			mwHttpLoop(&httpParam, 500);
			CheckChannels();
		} while (!httpParam.bKillWebserver && ShellWait(&proc, 0) == 0);
	}
	else if (ret == -1) {
		do {
			mwHttpLoop(&httpParam, 1000);
			CheckChannels();
		} while (!httpParam.bKillWebserver);
	}
	else if (ret == -2) {
		// child process (execve failed)
		return 0;
	}

	mwServerExit(&httpParam);
	return 0;
}
////////////////////////////// END OF FILE //////////////////////////////
