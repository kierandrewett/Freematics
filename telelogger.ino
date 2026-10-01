/******************************************************************************
* Arduino sketch of a vehicle data data logger and telemeter for Freematics Hub
* Works with Freematics ONE+ Model A and Model B
* Developed by Stanley Huang <stanley@freematics.com.au>
* Distributed under BSD license
* Visit https://freematics.com/products for hardware information
* Visit https://hub.freematics.com to view live and history telemetry data
*
* THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
* IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
* FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
* AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
* LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
* OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
* THE SOFTWARE.
******************************************************************************/

#include <FreematicsPlus.h>
#include <httpd.h>
#include <esp_sleep.h>
#include <sys/time.h>
#include <time.h>
#include "config.h"
#ifndef PREFER_CELLULAR
#define PREFER_CELLULAR 0
#endif
#include "telestore.h"
#include "teleclient.h"
#include "telequeue.h"
#include "sdaccess.h"
#if BOARD_HAS_PSRAM
#include "esp32/himem.h"
#endif
#include "driver/adc.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/portmacro.h"
#include "freertos/semphr.h"
#if ENABLE_OLED
#include "FreematicsOLED.h"
#endif

// states
#define STATE_STORAGE_READY 0x1
#define STATE_OBD_READY 0x2
#define STATE_GPS_READY 0x4
#define STATE_MEMS_READY 0x8
#define STATE_NET_READY 0x10
#define STATE_GPS_ONLINE 0x20
#define STATE_CELL_CONNECTED 0x40
#define STATE_WIFI_CONNECTED 0x80
#define STATE_WORKING 0x100
#define STATE_STANDBY 0x200

typedef struct {
  byte pid;
  byte priority;
  float value;
  uint32_t ts;
} PID_POLLING_INFO;

PID_POLLING_INFO obdData[]= {
#define OBD_PID(pid, name, description, unit, priority) {pid, priority, 0, 0},
#include "obd_pids.h"
};

typedef struct {
  byte mode;
  uint16_t countPid;
  uint16_t basePid;
  uint8_t count;
  uint16_t codes[DTC_CODE_SLOTS];
  uint32_t lastScan;
  uint16_t statusPid;
  uint8_t status;
} DTC_POLLING_INFO;

DTC_POLLING_INFO dtcData[] = {
  {0x03, PID_DTC_STORED_COUNT, PID_DTC_STORED_BASE, 0, {0}, 0, PID_DTC_STORED_STATUS, DTC_STATUS_NO_RESPONSE},
  {0x07, PID_DTC_PENDING_COUNT, PID_DTC_PENDING_BASE, 0, {0}, 0, PID_DTC_PENDING_STATUS, DTC_STATUS_NO_RESPONSE},
  {0x0A, PID_DTC_PERMANENT_COUNT, PID_DTC_PERMANENT_BASE, 0, {0}, 0, PID_DTC_PERMANENT_STATUS, DTC_STATUS_NO_RESPONSE},
};

CBufferManager bufman;
#if STORAGE == STORAGE_SD
DurableQueue durableQueue;
#endif
Task subtask;
Task obdTask;
Task gpsTask;
Task memsTask;
SemaphoreHandle_t memsMutex = nullptr;
Task recorderTask;
SemaphoreHandle_t coprocessorMutex = nullptr;
portMUX_TYPE sensorMux = portMUX_INITIALIZER_UNLOCKED;
GPS_DATA gpsSnapshot = {};
GPS_DATA gpsSample = {};
uint32_t voltageTimestamp = 0;
float cachedVoltage = 0;
struct MEMSSnapshot {
  float acceleration[3];
  float gyro[3];
  float compass[3];
  float temperature;
  ORIENTATION orientation;
  uint32_t timestamp;
};
MEMSSnapshot memsSnapshot = {};

// Extremes since the previous sample, gathered by the 50 Hz sensor loop. A
// 250 ms sample otherwise keeps only the latest reading, so a hard brake, a
// pothole or the engine-cranking voltage dip between samples is lost.
// Guarded by sensorMux; the sampler takes and resets it in one step, so every
// reading belongs to exactly one interval.
struct IntervalExtremes {
  float accPeak;      // largest |acceleration - bias| in g (gravity removed)
  float accAtPeak[3];
  float voltMin;
  float voltMax;
  uint16_t accReads;
  uint16_t voltReads;
};
IntervalExtremes intervalExtremes = {};

struct OBDSnapshot {
  PID_POLLING_INFO readings[sizeof(obdData) / sizeof(obdData[0])];
  DTC_POLLING_INFO diagnostics[sizeof(dtcData) / sizeof(dtcData[0])];
  uint32_t timeouts;
  uint32_t latency;
  uint16_t supported;
  uint8_t protocol;
  uint8_t failures;
  uint8_t status;
};
OBDSnapshot obdSnapshot = {};
// The few OBD values other tasks decide on. Copying the full 1.2 KB snapshot
// every 25 ms in the 3 KB status task risked its stack and kept interrupts
// off for longer than needed.
struct VehicleSignals {
  PID_POLLING_INFO rpm;
  PID_POLLING_INFO speed;
  uint32_t lastResponse; // latest successful OBD read, 0 before the first
  uint8_t status;
};
VehicleSignals vehicleSignals = {};

// After HTTP 400 on a batch, the refused frame is somewhere in the next
// `suspect` frames. Halving the batch finds it in about 2 * log2(n) requests.
struct ReplayIsolation {
  uint8_t suspect;
  uint8_t limit;
};
#if ENABLE_NETWORK_STATUS_SIGNALS || STORAGE == STORAGE_SD
Task statusTask;
#endif

#if ENABLE_MEMS
float accBias[3] = {0}; // calibrated reference accelerometer data
float accSum[3] = {0};
float acc[3] = {0};
float gyr[3] = {0};
float mag[3] = {0};
uint8_t accCount = 0;
#endif
int deviceTemp = 0;

// config data
char apn[32];
#if ENABLE_WIFI
char wifiSSID[32] = WIFI_SSID;
char wifiPassword[32] = WIFI_PASSWORD;
#endif
nvs_handle_t nvs;

bool persistConfigString(const char* key, const char* value)
{
  return nvs_set_str(nvs, key, value ? value : "") == ESP_OK && nvs_commit(nvs) == ESP_OK;
}

// live data
String netop;
String ip;
int16_t rssi = 0;
int16_t rssiLast = 0;
volatile uint32_t lastRssiMeasurement = 0;
char vin[18] = {0};
float batteryVoltage = 0;
float tripDistanceKm = 0;
float lastOBDSpeed = 0;
GPS_DATA* gd = 0;

char devid[12] = {0};
char isoTime[32] = {0};

// stats data
uint32_t lastMotionTime = 0;

// Vehicle power lifecycle. CONFIRMING records with the modem off until the
// car shows use; TRIP is full operation; WRAP_UP stops adding readings and
// uploads the rest; STANDBY is parked (see standby()).
#define PHASE_CONFIRMING 0
#define PHASE_TRIP 1
#define PHASE_WRAP_UP 2
#define PHASE_STANDBY 3
volatile uint8_t powerPhase = PHASE_CONFIRMING;
uint32_t phaseSince = 0;
bool vehicleActivitySeen = false;
// Why this boot started. It survives the wake reboot in RTC memory; the magic
// value rejects the random contents RTC memory has after a power cut.
#define WAKE_POWER_ON 0
#define WAKE_MOTION 1
#define WAKE_CHARGING 2
#define WAKE_MAGIC 0x57414B00UL
RTC_NOINIT_ATTR uint32_t wakeRecord;
uint8_t bootWakeReason = WAKE_POWER_ON;
volatile uint32_t lastCollectionTime = 0;
#if STORAGE == STORAGE_SD
volatile bool storageCheckComplete = false;
#endif
uint32_t lastOBDDistanceTime = 0;
uint32_t lastGPSDistanceTime = 0;
uint32_t timeoutsOBD = 0;
uint32_t timeoutsNet = 0;
uint32_t lastStatsTime = 0;
#if ENABLE_OBD
byte dtcScanIndex = 0;
byte fastOBDIndex = 0;
uint32_t lastOBDFastPoll = 0;
byte fastOBDFailureCycles = 0;
uint16_t supportedOBDPIDs = 0;
uint32_t lastOBDReadLatency = 0;
uint32_t lastOBDInitAttempt = 0;
#endif

int32_t syncInterval = SERVER_SYNC_INTERVAL * 1000;
int32_t dataInterval = 1000;

#if STORAGE != STORAGE_NONE
int fileid = 0;
uint16_t lastSizeKB = 0;
uint32_t lastLogFlush = 0;
#endif

byte ledMode = 0;
#if ENABLE_NETWORK_STATUS_SIGNALS
volatile bool telemetryTransmitActive = false;
#endif
// Set by the telemetry task once the parked marker is sent and the modem is off.
volatile bool telemetryParked = false;

bool serverSetup(IPAddress& ip);
void serverProcess(int timeout);
void processMEMS(CBuffer* buffer);
bool processGPS(CBuffer* buffer);
void processBLE(int timeout);
class State {
public:
  bool check(uint16_t flags)
  {
    portENTER_CRITICAL(&m_mux);
    bool result = (m_state & flags) == flags;
    portEXIT_CRITICAL(&m_mux);
    return result;
  }
  void set(uint16_t flags)
  {
    portENTER_CRITICAL(&m_mux);
    m_state |= flags;
    portEXIT_CRITICAL(&m_mux);
  }
  void clear(uint16_t flags)
  {
    portENTER_CRITICAL(&m_mux);
    m_state &= ~flags;
    portEXIT_CRITICAL(&m_mux);
  }
private:
  volatile uint16_t m_state = 0;
  portMUX_TYPE m_mux = portMUX_INITIALIZER_UNLOCKED;
};

FreematicsESP32 sys;

// OBD waits run in their own task. Never touch MEMS or BLE from here.
class OBD : public COBD {};

OBD obd;

MEMS_I2C* mems = 0;
bool hasMagnetometer = false;

#if STORAGE == STORAGE_SPIFFS
SPIFFSLogger logger;
#elif STORAGE == STORAGE_SD
SDLogger logger;
#endif

#if SERVER_PROTOCOL == PROTOCOL_UDP
TeleClientUDP teleClient;
#else
TeleClientHTTP teleClient;
#endif

#if ENABLE_OLED
OLED_SH1106 oled;
#endif

State state;

void printTimeoutStats()
{
  Serial.print("[ERRORS] OBD timeouts: ");
  Serial.print(timeoutsOBD);
  Serial.print(" | network timeouts: ");
  Serial.println(timeoutsNet);
}

#if ENABLE_OBD
void reportOBDReadFailure(byte pid, const char* tier)
{
  // A failing ECU can produce several failures in one polling cycle. One
  // rate-limited record gives the operator the cause without hiding useful
  // cellular, GNSS or power messages in repeated counter lines.
  static uint32_t lastReportTime = 0;
  const uint32_t now = millis();
  if (lastReportTime && now - lastReportTime < 10000UL) return;
  lastReportTime = now;
  Serial.print("[OBD] ");
  Serial.print(tier);
  Serial.print(" PID 0x");
  if (pid < 0x10) Serial.print('0');
  Serial.print(pid, HEX);
  Serial.print(" did not respond; total timeouts: ");
  Serial.println(timeoutsOBD);
}

void reportOBDCapabilities()
{
  byte supported = 0;
  byte fast = 0;
  byte auxiliary = 0;
  byte inventory = 0;
  const byte count = sizeof(obdData) / sizeof(obdData[0]);
  for (byte i = 0; i < count; i++) {
    if (!obd.isValidPID(obdData[i].pid)) continue;
    supported++;
    if (obdData[i].priority == 1) fast++;
    else if (obdData[i].priority == 2) auxiliary++;
    else inventory++;
  }
  supportedOBDPIDs = supported;
  Serial.print("[OBD] ECU supports ");
  Serial.print(supported);
  Serial.print(" tracked PIDs: ");
  Serial.print(fast);
  Serial.print(" fast, ");
  Serial.print(auxiliary);
  Serial.print(" auxiliary, ");
  Serial.print(inventory);
  Serial.println(" inventory");
}

void reportSlowOBDRead(byte pid, const char* tier, uint32_t elapsed)
{
  static uint32_t lastReportTime = 0;
  const uint32_t now = millis();
  if (lastReportTime && now - lastReportTime < 10000UL) return;
  lastReportTime = now;
  Serial.print("[OBD] ");
  Serial.print(tier);
  Serial.print(" PID 0x");
  if (pid < 0x10) Serial.print('0');
  Serial.print(pid, HEX);
  Serial.print(" response took ");
  Serial.print(elapsed);
  Serial.println(" ms");
}

void clearOBDReadings()
{
  // A new ECU session must not carry an old vehicle's values into the next
  // trip. Buffers already queued retain their original capture timestamp.
  memset(vin, 0, sizeof(vin));
  for (byte i = 0; i < sizeof(obdData) / sizeof(obdData[0]); i++) {
    obdData[i].value = 0;
    obdData[i].ts = 0;
  }
  memset(dtcData, 0, sizeof(dtcData));
  dtcData[0].mode = 0x03;
  dtcData[0].countPid = PID_DTC_STORED_COUNT;
  dtcData[0].basePid = PID_DTC_STORED_BASE;
  dtcData[0].statusPid = PID_DTC_STORED_STATUS;
  dtcData[0].status = DTC_STATUS_NO_RESPONSE;
  dtcData[1].mode = 0x07;
  dtcData[1].countPid = PID_DTC_PENDING_COUNT;
  dtcData[1].basePid = PID_DTC_PENDING_BASE;
  dtcData[1].statusPid = PID_DTC_PENDING_STATUS;
  dtcData[1].status = DTC_STATUS_NO_RESPONSE;
  dtcData[2].mode = 0x0A;
  dtcData[2].countPid = PID_DTC_PERMANENT_COUNT;
  dtcData[2].basePid = PID_DTC_PERMANENT_BASE;
  dtcData[2].statusPid = PID_DTC_PERMANENT_STATUS;
  dtcData[2].status = DTC_STATUS_NO_RESPONSE;
  dtcScanIndex = 0;
  fastOBDIndex = 0;
  lastOBDFastPoll = 0;
  fastOBDFailureCycles = 0;
  supportedOBDPIDs = 0;
  lastOBDReadLatency = 0;
}
#endif

void beepTone(unsigned int frequency, int duration)
{
    sys.buzzer(frequency);
    delay(duration);
    sys.buzzer(0);
}

// Use fresh speed, not engine RPM or vibration, to detect a driving trip.
// Unknown speed suppresses warnings but does not manufacture a trip end.
bool readTripMotion(bool& moving)
{
  VehicleSignals signals;
  GPS_DATA fix;
  portENTER_CRITICAL(&sensorMux);
  signals = vehicleSignals;
  fix = gpsSnapshot;
  portEXIT_CRITICAL(&sensorMux);
  const uint32_t now = millis();
  const PID_POLLING_INFO& speed = signals.speed;
  if (signals.status && speed.ts && now - speed.ts <= TRIP_SPEED_FRESH_MS &&
      isfinite(speed.value) && speed.value >= 0) {
    moving = speed.value >= TRIP_MOVING_SPEED_KPH;
    return true;
  }
  if (fix.ts && now - fix.ts <= TRIP_SPEED_FRESH_MS && fix.sat >= 4 &&
      fix.hdop > 0 && fix.hdop <= 5 && isfinite(fix.speed) && fix.speed >= 0 &&
      isfinite(fix.lat) && isfinite(fix.lng) && (fix.lat || fix.lng) &&
      fabsf(fix.lat) <= 90 && fabsf(fix.lng) <= 180) {
    moving = fix.speed * 1.852f >= TRIP_MOVING_SPEED_KPH;
    return true;
  }
  moving = false;
  return false;
}

void tripChime(bool started)
{
  Serial.println(started ? "[TRIP] Started" : "[TRIP] Stopped");
  // The Model B buzzer is driven at 2 kHz upstream; lower tones are too quiet
  // to hear over road noise. Three rising notes start a trip, three falling
  // notes end it, both distinct from the three equal warning beeps.
  const unsigned int notes[3] = {2000, 2600, 3200};
  for (uint8_t i = 0; i < 3; i++) {
    beepTone(notes[started ? i : 2 - i], 150);
    if (i < 2) delay(50);
  }
}

void recordingAlert(const char* message)
{
  Serial.println(message);
  for (uint8_t tone = 0; tone < 3; tone++) {
    beepTone(1600, 120);
    if (tone < 2) delay(100);
  }
}

#if ENABLE_NETWORK_STATUS_SIGNALS || STORAGE == STORAGE_SD
void statusSignals(void* inst)
{
  bool tripActive = false;
  uint32_t stoppedSince = 0;
  bool hadNetwork = false;
  bool everOnline = false;
  bool outageAnnounced = false;
  bool restoreChirpPending = false;
  uint32_t offlineSince = 0;
  uint32_t lastAlertAt = 0;
#if ENABLE_AUDIBLE_SERVER_ALERTS && SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
  uint32_t activeSince = 0;
  uint32_t observedServerResponse = 0;
  uint32_t lastServerResponse = 0;
  bool monitoringServer = false;
  bool serverAlerted = false;
#endif
#if STORAGE == STORAGE_SD
  const uint32_t recordingMonitorSince = millis();
  bool recordingFaultActive = false;
  uint32_t captureAtFault = 0;
  uint32_t lastRecordingAlertAt = 0;
#endif

  for (;;) {
    const uint32_t now = millis();
    const bool standbyMode = state.check(STATE_STANDBY);
    const bool working = state.check(STATE_WORKING);
    const bool cellOnline = state.check(STATE_NET_READY | STATE_CELL_CONNECTED);
    const bool wifiOnline = state.check(STATE_NET_READY | STATE_WIFI_CONNECTED);
    const bool networkOnline = cellOnline || wifiOnline;
    bool moving = false;
    const bool speedKnown = readTripMotion(moving);
    moving = moving && working && !standbyMode;
    if (moving) {
      stoppedSince = 0;
      if (!tripActive) {
        tripActive = true;
        tripChime(true);
      }
    } else if (tripActive) {
      if (speedKnown && !stoppedSince) stoppedSince = now;
      if (!speedKnown) stoppedSince = 0;
      if (standbyMode || powerPhase == PHASE_WRAP_UP ||
          (stoppedSince && now - stoppedSince >= TRIP_STOP_DELAY_MS)) {
        tripActive = false;
        stoppedSince = 0;
        tripChime(false);
      }
    }

#if ENABLE_AUDIBLE_SERVER_ALERTS && SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
    // Only an accepted telemetry batch updates lastDataSyncTime. A login or
    // ping cannot silence this alarm while recording remains unavailable.
    const uint32_t responseAt = teleClient.lastDataSyncTime;
    const uint32_t serverNow = millis();
    if (!tripActive) {
      monitoringServer = false;
      observedServerResponse = responseAt;
      serverAlerted = false;
    } else {
      if (!monitoringServer) {
        monitoringServer = true;
        activeSince = serverNow;
        lastServerResponse = 0;
      }
      if (responseAt && responseAt != observedServerResponse) {
        observedServerResponse = responseAt;
        lastServerResponse = responseAt;
        if (serverAlerted) Serial.println("[STATUS] Server response restored");
        serverAlerted = false;
      }
      const uint32_t contactSince = lastServerResponse ? lastServerResponse : activeSince;
      if (moving && !serverAlerted && serverNow - contactSince >= SERVER_RESPONSE_ALERT_MS) {
        serverAlerted = true;
        recordingAlert("[STATUS] No accepted telemetry for 60 seconds; three audible beeps");
      }
    }
#endif

#if STORAGE == STORAGE_SD
    // A known mount/write failure warns immediately. A blocked startup or
    // stopped collector gets 15 seconds. Upload acknowledgements cannot clear
    // this alarm. Warnings sound only while fresh speed shows movement.
    const uint32_t capturedAt = lastCollectionTime;
    // Read time after the cross-core capture timestamp, including time spent
    // sounding a server warning above. Unsigned subtraction must not see a
    // newer capture as a long recording stall.
    const uint32_t recordingNow = millis();
    const uint32_t progressAt = capturedAt ? capturedAt : recordingMonitorSince;
    const bool storageHealthy = durableQueue.healthy() && logger.healthy();
    const bool checked = storageCheckComplete ||
      recordingNow - recordingMonitorSince >= RECORDING_STALL_ALERT_MS;
    if (!standbyMode && checked) {
      const bool failed = !storageHealthy || recordingNow - progressAt >= RECORDING_STALL_ALERT_MS;
      if (failed && !recordingFaultActive) {
        recordingFaultActive = true;
        captureAtFault = capturedAt;
        lastRecordingAlertAt = 0;
      } else if (!failed && recordingFaultActive && capturedAt && capturedAt != captureAtFault) {
        recordingFaultActive = false;
        lastRecordingAlertAt = 0;
        Serial.println("[STATUS] Fresh local recording restored; fault alarm stopped");
      }
    }
    if (moving && recordingFaultActive &&
        (!lastRecordingAlertAt || recordingNow - lastRecordingAlertAt >= RECORDING_ALERT_REPEAT_MS)) {
      lastRecordingAlertAt = recordingNow;
      recordingAlert("[STATUS] Local recording failed; three beeps, repeating every five seconds");
    }
#endif

    if (networkOnline) {
      if (!hadNetwork) {
        // A reboot or a deliberate standby wake is not an outage.
        if (!everOnline) {
          Serial.println("[STATUS] Network online (initial)");
        } else {
          Serial.println("[STATUS] Network online");
        }
        everOnline = true;
      }
      // Pair one short restore chirp with the outage alert, without delaying
      // it behind the rate limit used for repeated outage alerts.
      if (restoreChirpPending) {
#if ENABLE_AUDIBLE_NETWORK_ALERTS
        if (moving) beepTone(2400, 80);
        Serial.println("[STATUS] Network restored (audible alert)");
#else
        Serial.println("[STATUS] Network restored");
#endif
        restoreChirpPending = false;
      }
      hadNetwork = true;
      offlineSince = 0;
      outageAnnounced = false;
    } else if (standbyMode || !working) {
      // Intentional modem shutdown is not a network outage.
      offlineSince = 0;
      outageAnnounced = false;
      restoreChirpPending = false;
    } else if (hadNetwork) {
      if (!offlineSince) offlineSince = now;
      if (!outageAnnounced && now - offlineSince >= NETWORK_ALERT_GRACE_MS) {
        Serial.println("[STATUS] Network offline for 15 seconds");
        const bool rateLimited = lastAlertAt &&
          now - lastAlertAt < NETWORK_ALERT_MIN_INTERVAL_MS;
        if (!rateLimited && moving) {
#if ENABLE_AUDIBLE_NETWORK_ALERTS
          beepTone(900, 140);
          delay(120);
          beepTone(900, 140);
          Serial.println("[STATUS] Audible outage alert");
#else
          Serial.println("[STATUS] Network outage recorded (buzzer disabled)");
#endif
          lastAlertAt = now;
        } else {
#if ENABLE_AUDIBLE_NETWORK_ALERTS
          Serial.println("[STATUS] Audible outage alert suppressed (rate limit)");
#else
          Serial.println("[STATUS] Network outage remains (buzzer disabled)");
#endif
        }
        outageAnnounced = true;
        // Only emit a matching restore chirp when the outage alert itself was
        // audible; rate-limited flaps stay silent in both directions.
#if ENABLE_AUDIBLE_NETWORK_ALERTS
        restoreChirpPending = !rateLimited && moving;
#else
        restoreChirpPending = false;
#endif
      }
    }

#if ENABLE_NETWORK_STATUS_SIGNALS && defined(PIN_LED)
    bool ledOn;
    if (standbyMode || powerPhase == PHASE_CONFIRMING) {
      // A parked or unconfirmed device must be visually and electrically quiet. Motion
      // wakes the loop; the normal online/upload indication resumes after
      // the active-mode restart.
      ledOn = false;
    } else if (networkOnline) {
      // Preserve the useful original behaviour: online flashes correspond to
      // a real HTTP request rather than an arbitrary heartbeat.
      ledOn = telemetryTransmitActive;
    } else if (hadNetwork) {
      const uint32_t phase = now % 1000UL;
      ledOn = phase % 250 < 100;
    } else {
      const uint32_t phase = now % 1000UL;
      ledOn = phase < 400;
    }
    digitalWrite(PIN_LED, ledOn ? HIGH : LOW);
#endif
    // There is no reason to service the status task at display cadence while
    // the device is parked and the LED is forced off.
    delay(standbyMode ? 1000 : 25);
  }
}
#endif

#if LOG_EXT_SENSORS
void processExtInputs(CBuffer* buffer)
{
#if LOG_EXT_SENSORS == 1
  uint8_t levels[2] = {(uint8_t)digitalRead(PIN_SENSOR1), (uint8_t)digitalRead(PIN_SENSOR2)};
  buffer->add(PID_EXT_SENSORS, ELEMENT_UINT8, levels, sizeof(levels), 2);
#elif LOG_EXT_SENSORS == 2
  uint16_t reading[] = {adc1_get_raw(ADC1_CHANNEL_0), adc1_get_raw(ADC1_CHANNEL_1)};
  Serial.print("GPIO0:");
  Serial.print((float)reading[0] * 3.15 / 4095 - 0.01);
  Serial.print(" GPIO1:");
  Serial.println((float)reading[1] * 3.15 / 4095 - 0.01);
  buffer->add(PID_EXT_SENSORS, ELEMENT_UINT16, reading, sizeof(reading), 2);
#endif
}
#endif

/*******************************************************************************
  HTTP API
*******************************************************************************/
#if ENABLE_HTTPD
int handlerLiveData(UrlHandlerParam* param)
{
    char *buf = param->pucBuffer;
    int bufsize = param->bufSize;
    int n = snprintf(buf, bufsize, "{\"obd\":{\"vin\":\"%s\",\"battery\":%.1f,\"pid\":[", vin, batteryVoltage);
    uint32_t t = millis();
    OBDSnapshot snapshot;
    portENTER_CRITICAL(&sensorMux);
    snapshot = obdSnapshot;
    portEXIT_CRITICAL(&sensorMux);
    for (int i = 0; i < sizeof(obdData) / sizeof(obdData[0]); i++) {
        n += snprintf(buf + n, bufsize - n, "{\"pid\":%u,\"value\":%d,\"age\":%u},",
            0x100 | snapshot.readings[i].pid, (int)snapshot.readings[i].value, (unsigned int)(t - snapshot.readings[i].ts));
    }
    n--;
    n += snprintf(buf + n, bufsize - n, "]}");
#if ENABLE_MEMS
    if (accCount) {
      n += snprintf(buf + n, bufsize - n, ",\"mems\":{\"acc\":[%d,%d,%d],\"stationary\":%u}",
          (int)((accSum[0] / accCount - accBias[0]) * 100), (int)((accSum[1] / accCount - accBias[1]) * 100), (int)((accSum[2] / accCount - accBias[2]) * 100),
          (unsigned int)(millis() - lastMotionTime));
    }
#endif
    if (gd && gd->ts) {
      n += snprintf(buf + n, bufsize - n, ",\"gps\":{\"utc\":\"%s\",\"lat\":%f,\"lng\":%f,\"alt\":%f,\"speed\":%f,\"sat\":%d,\"age\":%u}",
          isoTime, gd->lat, gd->lng, gd->alt, gd->speed, (int)gd->sat, (unsigned int)(millis() - gd->ts));
    }
    buf[n++] = '}';
    param->contentLength = n;
    param->contentType=HTTPFILETYPE_JSON;
    return FLAG_DATA_RAW;
}
#endif

/*******************************************************************************
  Reading and processing OBD data
*******************************************************************************/
#if ENABLE_OBD
void scanDiagnostics()
{
  DTC_POLLING_INFO& item = dtcData[dtcScanIndex];
  memset(item.codes, 0, sizeof(item.codes));
  item.count = obd.readDTC(item.mode, item.codes, DTC_CODE_SLOTS);
  item.status = obd.getDTCStatus();
  item.lastScan = millis();
  Serial.print("DTC mode ");
  Serial.print(item.mode, HEX);
  Serial.print(':');
  Serial.println(item.count);
  if (++dtcScanIndex >= sizeof(dtcData) / sizeof(dtcData[0])) dtcScanIndex = 0;
}



void updateOBDDistance(float speedKph)
{
  uint32_t now = millis();
  if (lastOBDDistanceTime && now - lastOBDDistanceTime < 5000 &&
      (!lastGPSDistanceTime || now - lastGPSDistanceTime > 5000)) {
    tripDistanceKm += (lastOBDSpeed + speedKph) * 0.5f *
      (now - lastOBDDistanceTime) / 3600000.0f;
  }
  lastOBDSpeed = speedKph;
  lastOBDDistanceTime = now;
}

void publishOBDSnapshot()
{
  // Only the OBD owner writes obdData/dtcData. Publish a bounded copy, never
  // hold a spinlock across serial I/O, delays or buffer serialisation.
  OBDSnapshot snapshot;
  memcpy(snapshot.readings, obdData, sizeof(obdData));
  memcpy(snapshot.diagnostics, dtcData, sizeof(dtcData));
  snapshot.timeouts = timeoutsOBD;
  snapshot.latency = lastOBDReadLatency;
  snapshot.supported = supportedOBDPIDs;
  snapshot.protocol = obd.getProtocol();
  snapshot.failures = fastOBDFailureCycles;
  snapshot.status = state.check(STATE_OBD_READY) ? (fastOBDFailureCycles ? 2 : 1) : 0;
  VehicleSignals signals = {};
  signals.status = snapshot.status;
  for (auto& item : obdData) {
    if (item.pid == PID_RPM) signals.rpm = item;
    if (item.pid == PID_SPEED) signals.speed = item;
    if (item.ts && (!signals.lastResponse || (int32_t)(item.ts - signals.lastResponse) > 0)) signals.lastResponse = item.ts;
  }
  portENTER_CRITICAL(&sensorMux);
  obdSnapshot = snapshot;
  vehicleSignals = signals;
  portEXIT_CRITICAL(&sensorMux);
}

void emitOBDSnapshot(CBuffer* buffer)
{
  OBDSnapshot snapshot;
  portENTER_CRITICAL(&sensorMux);
  snapshot = obdSnapshot;
  portEXIT_CRITICAL(&sensorMux);
  const uint32_t now = millis();
  for (auto& item : snapshot.readings) {
    if (!item.ts) continue; // never invent a value before its first response
    buffer->add(0x100 | item.pid, ELEMENT_FLOAT_D2, &item.value, sizeof(item.value));
    uint32_t age = now - item.ts;
    buffer->add(PID_OBD_AGE_BASE | item.pid, ELEMENT_UINT32, &age, sizeof(age));
    if (age <= 1500 && snapshot.status && item.pid == PID_SPEED) updateOBDDistance(item.value);
  }
  for (byte index = 0; index < sizeof(dtcData) / sizeof(dtcData[0]); index++) {
    auto& item = snapshot.diagnostics[index];
    if (item.lastScan) buffer->add(item.countPid, ELEMENT_UINT8, &item.count, sizeof(item.count));
    uint16_t statusPid = index == 0 ? PID_DTC_STORED_STATUS : index == 1 ? PID_DTC_PENDING_STATUS : PID_DTC_PERMANENT_STATUS;
    buffer->add(statusPid, ELEMENT_UINT8, &item.status, sizeof(item.status));
    if (!item.lastScan) continue;
    uint32_t age = now - item.lastScan;
    buffer->add(PID_DTC_AGE_BASE + index, ELEMENT_UINT32, &age, sizeof(age));
    for (byte i = 0; i < DTC_CODE_SLOTS; i++) buffer->add(item.basePid + i, ELEMENT_UINT16, item.codes + i, sizeof(item.codes[i]));
  }
  buffer->add(PID_OBD_PROTOCOL, ELEMENT_UINT8, &snapshot.protocol, sizeof(snapshot.protocol));
  buffer->add(PID_OBD_SUPPORTED_PIDS, ELEMENT_UINT16, &snapshot.supported, sizeof(snapshot.supported));
  buffer->add(PID_OBD_TIMEOUTS, ELEMENT_UINT32, &snapshot.timeouts, sizeof(snapshot.timeouts));
  buffer->add(PID_OBD_LAST_LATENCY, ELEMENT_UINT32, &snapshot.latency, sizeof(snapshot.latency));
  buffer->add(PID_OBD_STATE, ELEMENT_UINT8, &snapshot.status, sizeof(snapshot.status));
  buffer->add(PID_OBD_FAST_FAILURES, ELEMENT_UINT8, &snapshot.failures, sizeof(snapshot.failures));
}

void pollOBD()
{
  static byte auxIndex = 0;
  static uint32_t lastAuxPoll = 0;
  static bool cadenceReported = false;
  const byte count = sizeof(obdData) / sizeof(obdData[0]);

  if (!cadenceReported) {
    Serial.print("[OBD] Polling ");
    Serial.print(OBD_FAST_PIDS_PER_CYCLE);
    Serial.print(" fast PIDs every ");
    Serial.print(OBD_FAST_INTERVAL_MS);
    Serial.print(" ms; rotating auxiliary PIDs every ");
    Serial.print(OBD_AUX_INTERVAL_MS);
    Serial.println(" ms");
    cadenceReported = true;
  }

  const uint32_t now = millis();
  const bool fastDue = !lastOBDFastPoll || now - lastOBDFastPoll >= OBD_FAST_INTERVAL_MS;
  bool fastReadFailed = false;
  if (fastDue) {
    lastOBDFastPoll = now;
    lastOBDReadLatency = 0;
    byte sampled = 0;
    // Poll RPM and speed every fast cycle; rotate the remaining core PIDs.
    for (byte rpmIndex = 0; rpmIndex < count; rpmIndex++) {
      PID_POLLING_INFO& rpm = obdData[rpmIndex];
      if ((rpm.pid != PID_RPM && rpm.pid != PID_SPEED) || !obd.isValidPID(rpm.pid)) continue;
      sampled++;
      float value;
      const uint32_t readStarted = millis();
      const bool read = obd.readPID(rpm.pid, value);
      const uint32_t elapsed = millis() - readStarted;
      if (elapsed > lastOBDReadLatency) lastOBDReadLatency = elapsed;
      if (elapsed >= OBD_PID_READ_WARN_MS) reportSlowOBDRead(rpm.pid, "Fast", elapsed);
      if (!read) {
        timeoutsOBD++;
        reportOBDReadFailure(rpm.pid, "Fast");
        fastReadFailed = true;
      } else {
        rpm.ts = millis();
        rpm.value = value;
        publishOBDSnapshot();
      }
    }
    byte visited = 0;
    while (sampled < OBD_FAST_PIDS_PER_CYCLE && visited < count) {
      if (fastOBDIndex >= count) fastOBDIndex = 0;
      PID_POLLING_INFO& item = obdData[fastOBDIndex++];
      visited++;
      if (item.priority != 1 || item.pid == PID_RPM || item.pid == PID_SPEED || !obd.isValidPID(item.pid)) continue;
      sampled++;
      float value;
      const uint32_t readStarted = millis();
      const bool read = obd.readPID(item.pid, value);
      const uint32_t elapsed = millis() - readStarted;
      if (elapsed > lastOBDReadLatency) lastOBDReadLatency = elapsed;
      if (elapsed >= OBD_PID_READ_WARN_MS) reportSlowOBDRead(item.pid, "Fast", elapsed);
      if (!read) {
        timeoutsOBD++;
        reportOBDReadFailure(item.pid, "Fast");
        fastReadFailed = true;
        continue;
      }
      item.ts = millis();
      item.value = value;
      publishOBDSnapshot();

    }
  }

  if (fastDue && fastReadFailed) {
    if (++fastOBDFailureCycles >= MAX_OBD_ERRORS) {
      Serial.println("[OBD] Fast PID failures persisted; clearing ECU session");
      state.clear(STATE_OBD_READY);
    }
    // Do not add lower-priority traffic while the fast probe is unstable.
    return;
  }
  if (fastDue) fastOBDFailureCycles = 0;

  // Interleave auxiliary reads with the core schedule so a large burst cannot
  // stall collection. Bound attempted reads, including timeouts, so an ECU
  // that stops responding cannot turn one cycle into a full-catalogue scan.
  if (!lastAuxPoll || now - lastAuxPoll >= OBD_AUX_INTERVAL_MS) {
    lastAuxPoll = now;
    byte sampled = 0;
    byte visited = 0;
    while (sampled < OBD_AUX_PIDS_PER_CYCLE && visited < count) {
      if (auxIndex >= count) auxIndex = 0;
      PID_POLLING_INFO& item = obdData[auxIndex++];
      visited++;
      if (item.priority == 1 || !obd.isValidPID(item.pid)) continue;
      sampled++;
      float value;
      const uint32_t readStarted = millis();
      const bool read = obd.readPID(item.pid, value);
      const uint32_t elapsed = millis() - readStarted;
      if (elapsed > lastOBDReadLatency) lastOBDReadLatency = elapsed;
      if (elapsed >= OBD_PID_READ_WARN_MS) reportSlowOBDRead(item.pid, "Auxiliary", elapsed);
      if (!read) {
        timeoutsOBD++;
        reportOBDReadFailure(item.pid, "Auxiliary");
        continue;
      }
      item.ts = millis();
      item.value = value;
      publishOBDSnapshot();
    }
  }

  for (byte offset = 0; offset < sizeof(dtcData) / sizeof(dtcData[0]); offset++) {
    const byte index = (dtcScanIndex + offset) % (sizeof(dtcData) / sizeof(dtcData[0]));
    if (!dtcData[index].lastScan || millis() - dtcData[index].lastScan >= DTC_SCAN_INTERVAL_MS) {
      dtcScanIndex = index;
      scanDiagnostics();
      break;
    }
  }
}
#endif

bool initGPS()
{
  // start GNSS receiver
  if (sys.gpsBeginExt()) {
    Serial.println("GNSS:OK(E)");
  } else if (sys.gpsBegin()) {
    Serial.println("GNSS:OK(I)");
  } else {
    Serial.println("GNSS:NO");
    return false;
  }
  return true;
}

void emitGPSFields(CBuffer* buffer)
{
  if (!buffer || !gd) return;
  float kph = gd->speed * 1.852f;
  // Time, age and receiver quality also exist before a position fix.
  if (gd->date) buffer->add(PID_GPS_DATE, ELEMENT_UINT32, &gd->date, sizeof(uint32_t));
  buffer->add(PID_GPS_TIME, ELEMENT_UINT32, &gd->time, sizeof(uint32_t));
  buffer->add(PID_GPS_SAT_COUNT, ELEMENT_UINT8, &gd->sat, sizeof(uint8_t));
  buffer->add(PID_GPS_HDOP, ELEMENT_UINT8, &gd->hdop, sizeof(uint8_t));
  uint32_t age = millis() - gd->ts;
  buffer->add(PID_GPS_AGE, ELEMENT_UINT32, &age, sizeof(age));
  if ((!gd->lat && !gd->lng) || !isfinite(gd->lat) || !isfinite(gd->lng) ||
      gd->lat < -90 || gd->lat > 90 || gd->lng < -180 || gd->lng > 180) return;
  buffer->add(PID_GPS_LATITUDE, ELEMENT_FLOAT, &gd->lat, sizeof(float));
  buffer->add(PID_GPS_LONGITUDE, ELEMENT_FLOAT, &gd->lng, sizeof(float));
  buffer->add(PID_GPS_ALTITUDE, ELEMENT_FLOAT_D1, &gd->alt, sizeof(float));
  buffer->add(PID_GPS_SPEED, ELEMENT_FLOAT_D1, &kph, sizeof(kph));
  buffer->add(PID_GPS_HEADING, ELEMENT_UINT16, &gd->heading, sizeof(uint16_t));
}

void syncClockFromGPS(GPS_DATA* gd)
{
  // On some mobile networks NITZ and modem NTP are unavailable. A valid GNSS
  // position also supplies UTC; seed the ESP32 clock so the modem can use it
  // on its next HTTPS attempt without disabling certificate date checks.
  time_t currentUtc;
  time(&currentUtc);
  if (gd->date && gd->sat >= 4 &&
      gd->hdop > 0 && gd->hdop <= 5 &&
      isfinite(gd->lat) && isfinite(gd->lng) &&
      fabsf(gd->lat) <= 90 && fabsf(gd->lng) <= 180) {
    struct tm utc = {};
    utc.tm_year = (gd->date % 100) + 100;
    utc.tm_mon = ((gd->date / 100) % 100) - 1;
    utc.tm_mday = gd->date / 10000;
    utc.tm_hour = gd->time / 1000000;
    utc.tm_min = (gd->time / 10000) % 100;
    utc.tm_sec = (gd->time / 100) % 100;
    if (utc.tm_year >= 124 && utc.tm_year <= 137 &&
        utc.tm_mon >= 0 && utc.tm_mon < 12 && utc.tm_mday >= 1 && utc.tm_mday <= 31 &&
        utc.tm_hour < 24 && utc.tm_min < 60 && utc.tm_sec < 60) {
      const struct tm supplied = utc;
      // Newlib on this ESP32 toolchain exposes mktime but not timegm.
      setenv("TZ", "UTC0", 1);
      tzset();
      time_t seconds = mktime(&utc);
      struct tm check;
      if (seconds >= 1704067200 && gmtime_r(&seconds, &check) &&
          check.tm_year == supplied.tm_year && check.tm_mon == supplied.tm_mon &&
          check.tm_mday == supplied.tm_mday) {
        // A saved clock has no knowledge of time spent powered off. Refine it
        // from GNSS as well as seeding a previously unset clock.
        if (currentUtc < 1704067200 || llabs((long long)seconds - currentUtc) >= 2) {
          struct timeval tv = {seconds, 0};
          if (settimeofday(&tv, nullptr) == 0) Serial.println("[TIME] ESP32 clock synchronised from validated GNSS fix");
        }
        static uint32_t lastClockCheckpoint = 0;
        if (!lastClockCheckpoint || millis() - lastClockCheckpoint >= 3600000UL) {
          nvs_set_u32(nvs, "last_utc", seconds);
          nvs_commit(nvs);
          lastClockCheckpoint = millis();
        }
      }
    }
  }

}

bool processGPS(CBuffer* buffer)
{
  static uint32_t lastGPStime = 0;
  static uint32_t lastGPSdate = 0;
  static float lastGPSLat = 0;
  static float lastGPSLng = 0;

  if (!gd) {
    lastGPStime = 0;
    lastGPSLat = 0;
    lastGPSLng = 0;
  }
  portENTER_CRITICAL(&sensorMux);
  gpsSample = gpsSnapshot;
  portEXIT_CRITICAL(&sensorMux);
  gd = gpsSample.ts ? &gpsSample : nullptr;
  if (!gd) return false;
  emitGPSFields(buffer);
  const bool newFix = lastGPStime != gd->time || lastGPSdate != gd->date;
  if (!newFix) return false;
  if (gd->date) {
    // generate ISO time string
    char *p = isoTime + sprintf(isoTime, "%04u-%02u-%02uT%02u:%02u:%02u",
        (unsigned int)(gd->date % 100) + 2000, (unsigned int)(gd->date / 100) % 100, (unsigned int)(gd->date / 10000),
        (unsigned int)(gd->time / 1000000), (unsigned int)(gd->time % 1000000) / 10000, (unsigned int)(gd->time % 10000) / 100);
    unsigned char tenth = (gd->time % 100) / 10;
    if (tenth) p += sprintf(p, ".%c00", '0' + tenth);
    *p = 'Z';
    *(p + 1) = 0;
  }
  if ((!gd->lng && !gd->lat) || !isfinite(gd->lat) || !isfinite(gd->lng) ||
      gd->lat < -90 || gd->lat > 90 || gd->lng < -180 || gd->lng > 180) {
    // Time may be known before a position fix. Do not fabricate coordinates.
    return false;
  }

  float kph = gd->speed * 1.852f;
  // Preserve measured positions. Do not integrate distance across an outage
  // or from a stale fix; position rejection must not suppress telemetry.
  if ((lastGPSLat || lastGPSLng) && kph >= 1 &&
      millis() - lastGPSDistanceTime < 5000 && millis() - gd->ts < 1500) {
    float latDelta = (gd->lat - lastGPSLat) * DEG_TO_RAD;
    float lngDelta = (gd->lng - lastGPSLng) * DEG_TO_RAD;
    float x = lngDelta * cosf((gd->lat + lastGPSLat) * 0.5f * DEG_TO_RAD);
    float segmentKm = 6371.0f * sqrtf(x * x + latDelta * latDelta);
    if (segmentKm < 1) tripDistanceKm += segmentKm;
  }
  lastGPSLat = gd->lat;
  lastGPSLng = gd->lng;
  lastGPSDistanceTime = millis();

  state.set(STATE_GPS_ONLINE);
  lastGPStime = gd->time;
  lastGPSdate = gd->date;
  return true;
}

#if ENABLE_MEMS
void processMEMS(CBuffer* buffer)
{
  MEMSSnapshot snapshot;
  portENTER_CRITICAL(&sensorMux);
  snapshot = memsSnapshot;
  portEXIT_CRITICAL(&sensorMux);
  if (!buffer || !snapshot.timestamp) return;
  deviceTemp = (int)snapshot.temperature;
  memcpy(acc, snapshot.acceleration, sizeof(acc));
  memcpy(gyr, snapshot.gyro, sizeof(gyr));
  memcpy(mag, snapshot.compass, sizeof(mag));
  buffer->add(PID_ACC, ELEMENT_FLOAT_D2, snapshot.acceleration, sizeof(snapshot.acceleration), 3);
  buffer->add(PID_GYRO, ELEMENT_FLOAT_D2, snapshot.gyro, sizeof(snapshot.gyro), 3);
  // ICM-42627 has no magnetometer. Do not emit fabricated compass values.
  if (hasMagnetometer) buffer->add(PID_COMPASS, ELEMENT_FLOAT_D2, snapshot.compass, sizeof(snapshot.compass), 3);
#if ENABLE_ORIENTATION
  float orientation[3] = {snapshot.orientation.yaw, snapshot.orientation.pitch, snapshot.orientation.roll};
  buffer->add(PID_ORIENTATION, ELEMENT_FLOAT_D2, orientation, sizeof(orientation), 3);
#endif
  uint32_t age = millis() - snapshot.timestamp;
  buffer->add(PID_MEMS_AGE, ELEMENT_UINT32, &age, sizeof(age));
}

void noteAcceleration(IntervalExtremes& extremes, const float acceleration[3])
{
  const float magnitude = sqrtf(acceleration[0] * acceleration[0] + acceleration[1] * acceleration[1] +
                                acceleration[2] * acceleration[2]);
  if (!isfinite(magnitude)) return;
  if (!extremes.accReads || magnitude > extremes.accPeak) {
    extremes.accPeak = magnitude;
    memcpy(extremes.accAtPeak, acceleration, sizeof(extremes.accAtPeak));
  }
  if (extremes.accReads < UINT16_MAX) extremes.accReads++;
}

void noteVoltage(IntervalExtremes& extremes, float voltage)
{
  if (!isfinite(voltage)) return;
  if (!extremes.voltReads || voltage < extremes.voltMin) extremes.voltMin = voltage;
  if (!extremes.voltReads || voltage > extremes.voltMax) extremes.voltMax = voltage;
  if (extremes.voltReads < UINT16_MAX) extremes.voltReads++;
}

IntervalExtremes takeIntervalExtremes()
{
  IntervalExtremes extremes;
  portENTER_CRITICAL(&sensorMux);
  extremes = intervalExtremes;
  intervalExtremes = IntervalExtremes();
  portEXIT_CRITICAL(&sensorMux);
  return extremes;
}

// An interval with no sensor reads emits nothing, so a stale peak is never
// presented as new.
void emitIntervalExtremes(CBuffer* buffer)
{
  IntervalExtremes extremes = takeIntervalExtremes();
  if (extremes.accReads) {
    buffer->add(PID_ACC_PEAK, ELEMENT_FLOAT_D2, &extremes.accPeak, sizeof(extremes.accPeak));
    buffer->add(PID_ACC_PEAK_VECTOR, ELEMENT_FLOAT_D2, extremes.accAtPeak, sizeof(extremes.accAtPeak), 3);
  }
  if (extremes.voltReads) {
    uint16_t low = (uint16_t)(extremes.voltMin * 100);
    uint16_t high = (uint16_t)(extremes.voltMax * 100);
    buffer->add(PID_VOLTAGE_MIN, ELEMENT_UINT16, &low, sizeof(low));
    buffer->add(PID_VOLTAGE_MAX, ELEMENT_UINT16, &high, sizeof(high));
  }
}

void calibrateMEMS()
{
  xSemaphoreTake(memsMutex, portMAX_DELAY);
  if (state.check(STATE_MEMS_READY)) {
    accBias[0] = 0;
    accBias[1] = 0;
    accBias[2] = 0;
    int n;
    unsigned long t = millis();
    for (n = 0; millis() - t < 1000; n++) {
      float acc[3];
      if (!mems->read(acc)) continue;
      accBias[0] += acc[0];
      accBias[1] += acc[1];
      accBias[2] += acc[2];
      delay(10);
    }
    accBias[0] /= n;
    accBias[1] /= n;
    accBias[2] /= n;
    Serial.print("[MEMS] Calibration bias (x/y/z): ");
    Serial.print(accBias[0]);
    Serial.print('/');
    Serial.print(accBias[1]);
    Serial.print('/');
    Serial.println(accBias[2]);
  }
  xSemaphoreGive(memsMutex);
}
#endif

void printTime()
{
  time_t utc;
  time(&utc);
  struct tm *btm = gmtime(&utc);
  if (btm->tm_year > 100) {
    // valid system time available
    char buf[64];
    sprintf(buf, "%04u-%02u-%02u %02u:%02u:%02u",
      1900 + btm->tm_year, btm->tm_mon + 1, btm->tm_mday, btm->tm_hour, btm->tm_min, btm->tm_sec);
    Serial.print("UTC:");
    Serial.println(buf);
  }
}

float readVehicleVoltage()
{
#if ENABLE_OBD
  if (sys.devType > 12) return (float)(analogRead(A0) * 45) / 4095;
  float voltage;
  portENTER_CRITICAL(&sensorMux);
  voltage = cachedVoltage;
  portEXIT_CRITICAL(&sensorMux);
  return voltage;
#else
  return 0;
#endif
}

bool vehiclePowerPresent()
{
  return readVehicleVoltage() >= 6.0f;
}

#if ENABLE_CAN_CAPTURE && ENABLE_OBD && STORAGE != STORAGE_NONE
bool passiveCanCaptureComplete = false;

void capturePassiveCAN()
{
  if (passiveCanCaptureComplete) return;
  if (!state.check(STATE_OBD_READY | STATE_STORAGE_READY)) return;
  passiveCanCaptureComplete = true;

  SDGuard captureGuard;
  if (!captureGuard) return;
  const byte protocol = obd.getProtocol();
  if (protocol < 6 || protocol > 15) {
    Serial.println("[CAN] Passive capture skipped; OBD protocol is not CAN");
    return;
  }

  char raw[192];
  const uint32_t started = millis();
  uint16_t frames = 0;
  Serial.println("[CAN] Passive capture started; transmit disabled");
  obd.sniff(true);
  while (millis() - started < CAN_CAPTURE_DURATION_MS && frames < CAN_CAPTURE_MAX_FRAMES) {
    const int bytes = obd.receiveRawData(raw, sizeof(raw), CAN_CAPTURE_READ_TIMEOUT_MS);
    if (bytes <= 0) continue;
    const uint8_t storedBytes = bytes < CAN_CAPTURE_MAX_LINE_BYTES ? (uint8_t)bytes : CAN_CAPTURE_MAX_LINE_BYTES;
    logger.timestamp(millis());
    logger.logHex(PID_CAN_FRAME, (const uint8_t*)raw, storedBytes);
    frames++;
  }
  obd.sniff(false);
  logger.flush();
  Serial.print("[CAN] Passive capture complete; frames=");
  Serial.println(frames);
}
#endif

/*******************************************************************************
  Initializing all data logging components
*******************************************************************************/
void initialize()
{
  // Keep readings queued before standby or a component reinitialisation.
  tripDistanceKm = 0;
  lastOBDSpeed = 0;
  lastOBDDistanceTime = 0;
  lastGPSDistanceTime = 0;
  gd = 0;

#if ENABLE_MEMS
  if (state.check(STATE_MEMS_READY)) {
    mems->setLowPower(false);
    calibrateMEMS();
  }
#endif

#if STORAGE != STORAGE_NONE
  if (!state.check(STATE_STORAGE_READY)) {
    // init storage
    if (logger.init()) {
      state.set(STATE_STORAGE_READY);
#if STORAGE == STORAGE_SD
      durableQueue.begin();
#endif
    }
  }
  if (state.check(STATE_STORAGE_READY)) {
    fileid = logger.begin();
    if (!fileid) {
      logger.end();
      state.clear(STATE_STORAGE_READY);
      Serial.println("[STORAGE] Local logging unavailable");
    }
  }
#if STORAGE == STORAGE_SD
  storageCheckComplete = true;
#endif

#endif

  // check system time
  printTime();

  lastMotionTime = millis();
  powerPhase = PHASE_CONFIRMING;
  phaseSince = millis();
  vehicleActivitySeen = false;
  state.set(STATE_WORKING);

#if ENABLE_OLED
  delay(1000);
  oled.clear();
  oled.print("DEVICE ID: ");
  oled.println(devid);
  oled.setCursor(0, 7);
  oled.print("Packets");
  oled.setCursor(80, 7);
  oled.print("KB Sent");
  oled.setFontSize(FONT_SIZE_MEDIUM);
#endif
}

void showStats()
{
  uint32_t t = millis() - teleClient.startTime;
  char buf[32];
  sprintf(buf, "%02u:%02u.%c ", t / 60000, (t % 60000) / 1000, (t % 1000) / 100 + '0');
  Serial.print("[UPLOAD] Session: ");
  Serial.print(buf);
  Serial.print("| requests: ");
  Serial.print(teleClient.txCount);
  Serial.print(" | sent: ");
  Serial.print(teleClient.txBytes >> 10);
  Serial.print(" KB | received: ");
  Serial.print(teleClient.rxBytes);
  Serial.print(" bytes | average: ");
  Serial.print((unsigned int)((uint64_t)(teleClient.txBytes + teleClient.rxBytes) * 3600 / (millis() - teleClient.startTime)));
  Serial.print(" KB/hour");

  Serial.println();
#if ENABLE_OLED
  oled.setCursor(0, 2);
  oled.println(timestr);
  oled.setCursor(0, 5);
  oled.printInt(teleClient.txCount, 2);
  oled.setCursor(80, 5);
  oled.printInt(teleClient.txBytes >> 10, 3);
#endif
}

bool waitMotion(long timeout, float threshold = MOTION_THRESHOLD, uint8_t confirmationSamples = 1)
{
  unsigned long t = millis();
    uint8_t motionHits = 0;
    uint8_t ignitionHits = 0;
    uint8_t chargeHits = 0;
    bool restingSeen = false;
    const bool initiallyPowered = vehiclePowerPresent();
    do {
      // calculate relative movement
      float motion = 0;
      float acc[3];
      bool motionSensorReady = false;
#if ENABLE_MEMS
      motionSensorReady = mems && state.check(STATE_MEMS_READY) && mems->read(acc);
      if (motionSensorReady) {
      if (accCount == 10) {
        accCount = 0;
        accSum[0] = 0;
        accSum[1] = 0;
        accSum[2] = 0;
      }
      accSum[0] += acc[0];
      accSum[1] += acc[1];
      accSum[2] += acc[2];
      accCount++;
      for (byte i = 0; i < 3; i++) {
        float m = (acc[i] - accBias[i]);
        motion += m * m;
      }
      }
#endif
      // Never wake the ECU to check a parked vehicle. The Model B voltage input
      // is a passive ADC read, so it costs the car nothing.
      const float voltage = readVehicleVoltage();
      if (voltage >= 6.0f && voltage <= RESTING_VOLTAGE_MAX) restingSeen = true;
      if (!motionSensorReady) {
        // Without MEMS, the voltage input is the only wake source.
        if (voltage >= IGNITION_WAKE_VOLTAGE || (!initiallyPowered && voltage >= 6.0f)) {
          if (++ignitionHits >= IGNITION_WAKE_CONFIRM_SAMPLES) {
            Serial.println("[POWER] Motion sensor unavailable; passive ignition wake");
            wakeRecord = WAKE_MAGIC | WAKE_CHARGING;
            return true;
          }
        } else {
          ignitionHits = 0;
        }
      } else if (restingSeen && voltage >= IGNITION_WAKE_VOLTAGE) {
        // Resting to charging voltage means the engine has started, even if
        // the car has not moved yet. A charger holding the voltage up never
        // shows a resting reading, so it cannot cause repeated wakes.
        if (++chargeHits >= IGNITION_WAKE_CONFIRM_SAMPLES) {
          Serial.println("[POWER] Charging voltage after rest; engine started");
          wakeRecord = WAKE_MAGIC | WAKE_CHARGING;
          return true;
        }
      } else {
        chargeHits = 0;
      }
#if ENABLE_HTTPD
      serverProcess(100);
#endif
      // Do not add an avoidable delay when BLE is disabled in production.
      processBLE(0);
      // check movement
      if (motion >= threshold * threshold) {
        if (motionHits < confirmationSamples) motionHits++;
        if (motionHits >= confirmationSamples) {
          //lastMotionTime = millis();
          Serial.print("[POWER] Motion confirmed; wake score: ");
          Serial.println(motion);
          wakeRecord = WAKE_MAGIC | WAKE_MOTION;
          return true;
        }
      } else {
        motionHits = 0;
      }

      // Keep the MEMS polling wake-up responsive without leaving the ESP32
      // CPU spinning for the entire parked interval. The timer wake-up is
      // deliberately short so real vehicle movement is acted on promptly.
      esp_sleep_enable_timer_wakeup((uint64_t)STANDBY_POLL_INTERVAL_MS * 1000ULL);
      esp_light_sleep_start();
    } while (state.check(STATE_STANDBY) && ((long)(millis() - t) < timeout || timeout == -1));
    return false;
}

/*******************************************************************************
  Collecting and processing data
*******************************************************************************/
void collectSample()
{
  uint32_t startTime = millis();

  CBuffer* buffer = bufman.getFree();
  if (!buffer) {
    bufman.recordMissedReading();
    static uint32_t lastWarning = 0;
    if (!lastWarning || millis() - lastWarning >= 10000UL) {
      Serial.println("[CRITICAL] No RAM slot or durable storage; new readings cannot be captured");
      lastWarning = millis();
    }
    delay(50);
    return;
  }

#if ENABLE_OBD
  emitOBDSnapshot(buffer);
#endif

  {
    int val = (rssiLast = rssi);
    buffer->add(PID_CSQ, ELEMENT_INT32, &val, sizeof(val));
    uint32_t age = lastRssiMeasurement ? millis() - lastRssiMeasurement : 0xFFFFFFFF;
    buffer->add(PID_CSQ_AGE, ELEMENT_UINT32, &age, sizeof(age));
  }
  uint8_t networkTransport = state.check(STATE_CELL_CONNECTED) ? 2 :
    (state.check(STATE_WIFI_CONNECTED) ? 1 : 0);
  buffer->add(PID_NETWORK_TRANSPORT, ELEMENT_UINT8, &networkTransport, sizeof(networkTransport));
#if ENABLE_OBD
  batteryVoltage = readVehicleVoltage();
  if (batteryVoltage || sys.devType > 12) {
    uint16_t v = batteryVoltage * 100;
    buffer->add(PID_BATTERY_VOLTAGE, ELEMENT_UINT16, &v, sizeof(v));
    uint32_t ts;
    portENTER_CRITICAL(&sensorMux);
    ts = voltageTimestamp;
    portEXIT_CRITICAL(&sensorMux);
    uint32_t age = sys.devType > 12 ? 0 : millis() - ts;
    buffer->add(PID_VOLTAGE_AGE, ELEMENT_UINT32, &age, sizeof(age));
  }
#endif

#if LOG_EXT_SENSORS
  processExtInputs(buffer);
#endif

#if ENABLE_MEMS
  processMEMS(buffer);
#endif
  emitIntervalExtremes(buffer);

  processGPS(buffer);

  buffer->add(PID_TRIP_DISTANCE, ELEMENT_FLOAT_D2, &tripDistanceKm, sizeof(tripDistanceKm));

  if (!state.check(STATE_MEMS_READY)) {
    deviceTemp = readChipTemperature();
  }
  buffer->add(PID_DEVICE_TEMP, ELEMENT_INT32, &deviceTemp, sizeof(deviceTemp));
  // Queue fields describe backlog before the current sample enters the queue.
  uint16_t queuedReadings = bufman.pendingReadings();
  uint32_t queuedBytes = bufman.pendingBytes();
  buffer->add(PID_QUEUE_READINGS, ELEMENT_UINT16, &queuedReadings, sizeof(queuedReadings));
  buffer->add(PID_QUEUE_BYTES, ELEMENT_UINT32, &queuedBytes, sizeof(queuedBytes));
  uint32_t missedReadings = bufman.missedReadings();
  buffer->add(PID_MISSED_READINGS, ELEMENT_UINT32, &missedReadings, sizeof(missedReadings));
#if STORAGE == STORAGE_SD
  uint32_t durableBytes = durableQueue.cachedPendingBytes();
  buffer->add(PID_DURABLE_QUEUE_BYTES, ELEMENT_UINT32, &durableBytes, sizeof(durableBytes));
  uint8_t queueHealthy = durableQueue.cachedHealthy() ? 1 : 0;
  buffer->add(PID_DURABLE_QUEUE_HEALTH, ELEMENT_UINT8, &queueHealthy, sizeof(queueHealthy));
  uint32_t rejected = durableQueue.rejectedCount();
  buffer->add(PID_REJECTED_READINGS, ELEMENT_UINT32, &rejected, sizeof(rejected));
#endif
  {
    uint8_t phase = powerPhase;
    buffer->add(PID_POWER_PHASE, ELEMENT_UINT8, &phase, sizeof(phase));
    buffer->add(PID_WAKE_REASON, ELEMENT_UINT8, &bootWakeReason, sizeof(bootWakeReason));
  }

  buffer->timestamp = startTime;

  bufman.publish(buffer);
  lastCollectionTime = millis();

}

// Engine or wheels in use, from the latest acquired values. Engine RPM counts,
// so idling in traffic keeps full-rate recording. GNSS needs a good fix, so
// parked jitter cannot hold the device awake. Also reports the time of the
// latest successful OBD response (0 when the ECU has not answered).
bool vehicleActivityNow(uint32_t now, uint32_t* lastOBDResponse)
{
  VehicleSignals signals;
  GPS_DATA fix;
  portENTER_CRITICAL(&sensorMux);
  signals = vehicleSignals;
  fix = gpsSnapshot;
  portEXIT_CRITICAL(&sensorMux);
  *lastOBDResponse = signals.lastResponse;
  auto fresh = [&](const PID_POLLING_INFO& item, float minimum) {
    return signals.status && item.ts && now - item.ts <= 1500 && isfinite(item.value) && item.value >= minimum;
  };
  bool active = fresh(signals.rpm, 100) || fresh(signals.speed, 2);
  if (fix.ts && now - fix.ts <= 1500 && fix.sat >= 4 && fix.hdop > 0 && fix.hdop <= 5 &&
      isfinite(fix.speed) && fix.speed * 1.852f >= 2) active = true;
  return active;
}

// The trip lifecycle as one decision on plain values, so
// tools/check-device-lifecycle.py can drive it. Returns the next phase.
uint8_t nextPowerPhase(uint8_t phase, uint32_t now, uint32_t since, uint32_t lastActivity,
                       bool activitySeen, uint32_t lastOBDResponse, float voltage,
                       uint32_t backlogBytes, uint16_t ramOnly)
{
  const bool vehiclePower = voltage >= 6.0f;
  if (phase == PHASE_CONFIRMING) {
    // USB bench power has no car battery to protect: behave as a trip.
    if (!vehiclePower || activitySeen) return PHASE_TRIP;
    // A wake with no engine or movement goes back to sleep without ever
    // powering the modem.
    return now - since >= CONFIRM_WINDOW_MS ? PHASE_STANDBY : phase;
  }
  if (phase == PHASE_TRIP) {
    const uint32_t idle = now - lastActivity;
    // Ignition off: the ECU has gone quiet, nothing moves, and the alternator
    // is not charging. At a red light the ECU keeps answering.
    const bool ecuQuiet = lastOBDResponse && now - lastOBDResponse >= OBD_SILENT_MS;
    if (vehiclePower && ecuQuiet && idle >= CAR_OFF_CONFIRM_MS && voltage < IGNITION_WAKE_VOLTAGE) {
      return PHASE_WRAP_UP;
    }
    // Fallback for an ECU that keeps answering with the engine off, or a car
    // without OBD data.
    if (idle < STANDBY_AFTER_STATIONARY_MS) return phase;
    // On the bench, keep uploading the backlog instead.
    if (!vehiclePower && (backlogBytes || ramOnly)) return phase;
    return PHASE_WRAP_UP;
  }
  if (phase == PHASE_WRAP_UP) {
    // Activity after the trip ended (a stop-start engine, or pulling away):
    // the trip continues and recording resumes.
    if ((int32_t)(lastActivity - since) > 0) return PHASE_TRIP;
    if (!backlogBytes && !ramOnly) return PHASE_STANDBY;
    return now - since >= UPLOAD_WINDOW_MS ? PHASE_STANDBY : phase;
  }
  return phase;
}

void process()
{
  static TickType_t deadline = xTaskGetTickCount();
  const uint32_t now = millis();
  uint32_t lastOBDResponse = 0;
  if (vehicleActivityNow(now, &lastOBDResponse)) {
    lastMotionTime = now;
    vehicleActivitySeen = true;
  }
  uint32_t backlogBytes = 0;
#if STORAGE == STORAGE_SD
  backlogBytes = durableQueue.cachedPendingBytes();
#endif
  const uint8_t next = nextPowerPhase(powerPhase, now, phaseSince, lastMotionTime, vehicleActivitySeen,
                                      lastOBDResponse, readVehicleVoltage(), backlogBytes,
                                      bufman.unpersistedReadings());
  if (next != powerPhase) {
    static const char* const names[] = {"confirming", "trip", "wrap-up", "standby"};
    Serial.print("[POWER] ");
    Serial.print(names[powerPhase]);
    Serial.print(" -> ");
    Serial.println(names[next]);
    if (next == PHASE_STANDBY) {
      state.clear(STATE_WORKING);
      return;
    }
    powerPhase = next;
    phaseSince = now;
  }
  if (powerPhase == PHASE_WRAP_UP) {
    // The trip is over. Stop adding readings so the upload window can empty
    // the backlog; the acquisition tasks keep watching for a resumed trip.
    vTaskDelayUntil(&deadline, pdMS_TO_TICKS(SAMPLE_INTERVAL_MS));
    return;
  }
  collectSample();
  dataInterval = SAMPLE_INTERVAL_MS;
  // Absolute deadlines avoid accumulating sensor/formatting time as drift.
  // If overloaded, skip expired slots rather than inventing catch-up samples.
  const uint32_t elapsed = xTaskGetTickCount() - deadline;
  if (elapsed >= pdMS_TO_TICKS(SAMPLE_INTERVAL_MS)) {
    bufman.recordMissedReading(elapsed / pdMS_TO_TICKS(SAMPLE_INTERVAL_MS));
    static uint32_t lastWarning = 0;
    if (!lastWarning || millis() - lastWarning >= 10000UL) {
      Serial.println("[SAMPLING] Deadline missed; inspect sensor age, queue and missed-reading metrics");
      lastWarning = millis();
    }
    deadline = xTaskGetTickCount();
  }
  vTaskDelayUntil(&deadline, pdMS_TO_TICKS(SAMPLE_INTERVAL_MS));
  processBLE(0);
}

void recordSamples(void*)
{
  for (;;) {
    if (!state.check(STATE_WORKING)) { delay(50); continue; }
#if STORAGE == STORAGE_SD
  static uint32_t lastRecovery = 0;
  if (durableQueue.damaged() &&
      (!lastRecovery || millis() - lastRecovery >= 5000UL)) {
    lastRecovery = millis();
    durableQueue.recover();
  }
  // A failed boot mount otherwise leaves the device on its finite RAM queue
  // for the entire active session. Retry at a bounded rate; an already-open
  // logger must not be reopened just because the journal was unavailable.
  static uint32_t lastSDRetry = 0;
  if ((!state.check(STATE_STORAGE_READY) || !logger.healthy() || !durableQueue.healthy()) &&
      (lastSDRetry == 0 || millis() - lastSDRetry >= 30000UL)) {
    lastSDRetry = millis();
    Serial.println("[STORAGE] Retrying SD storage");
    SDGuard recovery;
    if (recovery) {
      // Recovery can replay an already accepted batch. It cannot advance past
      // the saved cursor or discard a RAM sample while the card is unavailable.
      logger.end();
      durableQueue.suspend();
      state.clear(STATE_STORAGE_READY);
      SD.end();
      SPI.end();
      delay(100);
      if (logger.init()) {
        if (durableQueue.begin()) Serial.println("[STORAGE] SD journal recovered; replay enabled");
        fileid = logger.begin();
        if (fileid) state.set(STATE_STORAGE_READY);
      }
    }
  }
#endif

#if STORAGE == STORAGE_SD
  logger.maintain();
#endif
    if (millis() - lastStatsTime >= 3000) {
      bufman.printStats();
      lastStatsTime = millis();
    }
#if STORAGE == STORAGE_SD
    // Take every reading waiting in RAM, up to one SD transaction's worth.
    // One open, flush and verify per batch keeps the recorder ahead of 4 Hz
    // even after a stall; per-reading transactions let RAM fill on the road.
    static char* frameBlock = (char*)heap_caps_malloc(RECORD_BLOCK_SIZE, MALLOC_CAP_SPIRAM);
    CBuffer* batch[RECORD_BATCH_MAX];
    const char* frames[RECORD_BATCH_MAX];
    uint16_t lengths[RECORD_BATCH_MAX];
    uint8_t count = 0;
    uint32_t used = 0;
    // Once storage recovers, journal RAM fallback readings even while offline.
    const bool journalOpen = frameBlock && durableQueue.ready() && durableQueue.healthy();
    while (count < RECORD_BATCH_MAX) {
      CBuffer* buffer = bufman.getOldest(false);
      if (!buffer && journalOpen) buffer = bufman.getOldest(true);
      if (!buffer) break;
      if (frameBlock) {
        CStorageRAM frame;
        frame.init(frameBlock + used, min((uint32_t)SAMPLE_FRAME_SIZE, (uint32_t)(RECORD_BLOCK_SIZE - used)));
        frame.timestamp(buffer->timestamp);
        buffer->serialize(frame);
        if (frame.overflowed()) {
          // The block is full. This reading goes first in the next batch.
          if (count) { bufman.restore(buffer); break; }
          lengths[count] = 0;
        } else {
          frames[count] = frameBlock + used;
          lengths[count] = frame.length();
          used += frame.length();
        }
      } else {
        lengths[count] = 0;
      }
      batch[count++] = buffer;
      if (!lengths[count - 1]) break;
    }
    if (!count) { delay(5); continue; }

    if (state.check(STATE_STORAGE_READY)) {
      SDGuard logGuard;
      for (uint8_t i = 0; i < count; i++) {
        if (batch[i]->recorded) continue;
        logger.timestamp(batch[i]->timestamp);
        batch[i]->serialize(logger);
      }
      // The journal is the durable copy; the CSV log only needs a periodic
      // flush, not one SD sync per reading.
      uint16_t sizeKB = (uint16_t)(logger.size() >> 10);
      if (sizeKB != lastSizeKB && millis() - lastLogFlush >= LOG_FLUSH_INTERVAL_MS) {
        logger.flush();
        lastLogFlush = millis();
        lastSizeKB = sizeKB;
        Serial.print("[FILE] ");
        Serial.print(sizeKB);
        Serial.println("KB");
      }
    }

    const bool journaled = lengths[count - 1] && durableQueue.ready() &&
      durableQueue.appendBatch(frames, lengths, count);
    for (uint8_t i = 0; i < count; i++) {
      // The SD journal owns these readings. RAM stays the fallback if the
      // card write fails, and a RAM copy is never logged to CSV twice.
      if (journaled) bufman.free(batch[i]);
      else { batch[i]->recorded = true; bufman.restore(batch[i]); }
    }
#else
    CBuffer* buffer = bufman.getOldest(false);
    if (!buffer) { delay(5); continue; }
#if STORAGE != STORAGE_NONE
  if (state.check(STATE_STORAGE_READY)) {
    logger.timestamp(buffer->timestamp);
    buffer->serialize(logger);
#if STORAGE == STORAGE_SPIFFS
    if (logger.size() >= SPIFFS_MAX_FILE_BYTES) {
      logger.end();
      fileid = logger.begin();
      lastSizeKB = 0;
    }
#endif
    uint16_t sizeKB = (uint16_t)(logger.size() >> 10);
    if (sizeKB != lastSizeKB) {
      logger.flush();
      lastSizeKB = sizeKB;
      Serial.print("[FILE] ");
      Serial.print(sizeKB);
      Serial.println("KB");
    }
  }
#endif
  buffer->recorded = true;
  bufman.restore(buffer);
#endif
  }
}

void acquireOBD(void*)
{
#if ENABLE_OBD
  for (;;) {
    if (!state.check(STATE_WORKING)) { delay(50); continue; }
    xSemaphoreTake(coprocessorMutex, portMAX_DELAY);
    const uint32_t now = millis();
    if (!state.check(STATE_OBD_READY) || obd.errors >= MAX_OBD_ERRORS) {
      state.clear(STATE_OBD_READY);
      publishOBDSnapshot();
      if (!lastOBDInitAttempt || now - lastOBDInitAttempt >= OBD_RETRY_INTERVAL_MS) {
        lastOBDInitAttempt = now;
        if (vehiclePowerPresent() && obd.init(PROTO_AUTO, true)) {
          state.set(STATE_OBD_READY);
          fastOBDFailureCycles = 0;
          reportOBDCapabilities();
          char buf[128];
          if (obd.getVIN(buf, sizeof(buf))) {
            strncpy(vin, buf, sizeof(vin) - 1);
            vin[sizeof(vin) - 1] = 0;
          }
          Serial.println("[OBD] ECU connected (background)");
#if ENABLE_CAN_CAPTURE && STORAGE != STORAGE_NONE
          capturePassiveCAN();
#endif
        }
      }
    } else {
      pollOBD();
    }
    // Model B voltage is an ADC read. Older boards use the serial link.
    float voltage = sys.devType > 12 ? readVehicleVoltage() : obd.getVoltage();
    portENTER_CRITICAL(&sensorMux);
    if (voltage > 0) { cachedVoltage = voltage; voltageTimestamp = millis(); }
    portEXIT_CRITICAL(&sensorMux);
    publishOBDSnapshot();
    xSemaphoreGive(coprocessorMutex);
    delay(10);
  }
#endif
  vTaskDelete(nullptr);
}

void acquireGPS(void*)
{
#if GNSS == GNSS_STANDALONE
  uint32_t lastFix = millis();
  for (;;) {
    if (!state.check(STATE_WORKING)) { delay(50); continue; }
    if (!state.check(STATE_GPS_READY)) {
      if (xSemaphoreTake(coprocessorMutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        if (initGPS()) state.set(STATE_GPS_READY);
        lastFix = millis();
        xSemaphoreGive(coprocessorMutex);
      }
    } else {
      const bool usesLink = sys.gpsUsesLink();
      // External UART GNSS keeps running even through a long OBD timeout.
      if (!usesLink || xSemaphoreTake(coprocessorMutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        GPS_DATA* fix = nullptr;
        if (sys.gpsGetData(&fix) && fix && fix->ts) {
          bool fresh = false;
          portENTER_CRITICAL(&sensorMux);
          if (!gpsSnapshot.ts || fix->date != gpsSnapshot.date || fix->time != gpsSnapshot.time) {
            fresh = true;
            gpsSnapshot = *fix;
            lastFix = millis();
          }
          portEXIT_CRITICAL(&sensorMux);
          if (fresh) syncClockFromGPS(fix);
        }
        if (usesLink) xSemaphoreGive(coprocessorMutex);
      }
#if GNSS_RESET_TIMEOUT
      if (millis() - lastFix > GNSS_RESET_TIMEOUT * 1000UL &&
          xSemaphoreTake(coprocessorMutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        sys.gpsEnd();
        state.clear(STATE_GPS_READY | STATE_GPS_ONLINE);
        lastFix = millis();
        xSemaphoreGive(coprocessorMutex);
      }
#endif
    }
    delay(20);
  }
#endif
  vTaskDelete(nullptr);
}

void acquireMEMS(void*)
{
#if ENABLE_MEMS
  uint8_t failures = 0;
  uint32_t lastRetry = 0;
  for (;;) {
    if (!state.check(STATE_WORKING)) { delay(50); continue; }
#if ENABLE_OBD
    // The Model B voltage input is a passive ADC read. Sampling it here at
    // 50 Hz catches the cranking dip whether or not the motion sensor works.
    if (sys.devType > 12) {
      const float voltage = readVehicleVoltage();
      portENTER_CRITICAL(&sensorMux);
      noteVoltage(intervalExtremes, voltage);
      portEXIT_CRITICAL(&sensorMux);
    }
#endif
    if (!mems) { delay(20); continue; }
    if (!state.check(STATE_MEMS_READY)) {
      if (!lastRetry || millis() - lastRetry >= 5000UL) {
        lastRetry = millis();
        xSemaphoreTake(memsMutex, portMAX_DELAY);
        mems->end();
#if ENABLE_ORIENTATION
        const bool recovered = mems->begin(true);
#else
        const bool recovered = mems->begin(false);
#endif
        xSemaphoreGive(memsMutex);
        if (recovered) {
          failures = 0;
          state.set(STATE_MEMS_READY);
          Serial.println("[MEMS] Sensor recovered; acquisition resumed");
        }
      }
      if (!state.check(STATE_MEMS_READY)) { delay(20); continue; }
    }
    xSemaphoreTake(memsMutex, portMAX_DELAY);
    MEMSSnapshot snapshot = {};
    bool success = mems->read(snapshot.acceleration, snapshot.gyro, snapshot.compass,
                            &snapshot.temperature,
#if ENABLE_ORIENTATION
                            &snapshot.orientation
#else
                            nullptr
#endif
                            );
    xSemaphoreGive(memsMutex);
    if (success) {
      failures = 0;
      for (byte i = 0; i < 3; i++) snapshot.acceleration[i] -= accBias[i];
      snapshot.timestamp = millis();
      portENTER_CRITICAL(&sensorMux);
      memsSnapshot = snapshot;
      noteAcceleration(intervalExtremes, snapshot.acceleration);
      portEXIT_CRITICAL(&sensorMux);
    } else if (++failures >= 10) {
      state.clear(STATE_MEMS_READY);
      Serial.println("[MEMS] Repeated read failures; retrying sensor initialisation");
    }
    delay(20);
  }
#endif
  vTaskDelete(nullptr);
}

bool initCell(bool quick = false)
{
  Serial.println("[CELL] Activating...");
  // power on network module
  if (!teleClient.cell.begin(&sys)) {
    Serial.println("[CELL] No supported module");
#if ENABLE_OLED
    oled.println("No Cell Module");
#endif
    return false;
  }
  if (quick) return true;
#if ENABLE_OLED
    oled.print(teleClient.cell.deviceName());
    oled.println(" OK\r");
    oled.print("IMEI:");
    oled.println(teleClient.cell.IMEI);
#endif
  Serial.print("CELL:");
  Serial.println(teleClient.cell.deviceName());
  if (!teleClient.cell.checkSIM(SIM_CARD_PIN)) {
    Serial.println("NO SIM CARD");
    teleClient.cell.end();
    return false;
  }
  Serial.print("IMEI:");
  Serial.println(teleClient.cell.IMEI);
  Serial.println("[CELL] Searching...");
  if (*apn) {
    Serial.print("APN:");
    Serial.println(apn);
  }
  if (teleClient.cell.setup(apn, APN_USERNAME, APN_PASSWORD)) {
    netop = teleClient.cell.getOperatorName();
    if (netop.length()) {
      Serial.print("Operator:");
      Serial.println(netop);
#if ENABLE_OLED
      oled.println(op);
#endif
    }

#if GNSS == GNSS_CELLULAR
    if (teleClient.cell.setGPS(true)) {
      Serial.println("CELL GNSS:OK");
    }
#endif

    ip = teleClient.cell.getIP();
    if (ip.length()) {
      Serial.print("[CELL] IP:");
      Serial.println(ip);
#if ENABLE_OLED
      oled.print("IP:");
      oled.println(ip);
#endif
    }
    state.set(STATE_CELL_CONNECTED);
  } else {
    char *p = strstr(teleClient.cell.getBuffer(), "+CPSI:");
    if (p) {
      char *q = strchr(p, '\r');
      if (q) *q = 0;
      Serial.print("[CELL] ");
      Serial.println(p + 7);
#if ENABLE_OLED
      oled.println(p + 7);
#endif
    } else {
      Serial.print(teleClient.cell.getBuffer());
    }
  }
  timeoutsNet = 0;
  return state.check(STATE_CELL_CONNECTED);
}

/*******************************************************************************
  Initializing network, maintaining connection and doing transmissions
*******************************************************************************/
#if STORAGE == STORAGE_SD && SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
// Fill one upload batch from the SD journal and return its frame count.
// The journal survives reboots, and each reboot restarts the device clock.
// The collector refuses a batch whose timestamps go backwards, so a clock
// reset always starts a new batch.
uint8_t buildReplayBatch(DurableQueue& queue, CStorageRAM& store, char* frame, uint16_t capacity,
                         uint8_t limit, uint16_t* lastLength)
{
  uint8_t count = 0;
  uint32_t previousTick = 0;
  const uint32_t started = millis();
  while (count < limit) {
    const uint32_t position = queue.readPosition();
    uint16_t length = 0;
    if (!queue.peek(frame, capacity, &length)) {
      if (count && millis() - started < HTTP_BATCH_MAX_WAIT_MS) {
        delay(25);
        continue;
      }
      break;
    }
    const bool timed = length > 2 && frame[0] == '0' && frame[1] == ':';
    const uint32_t tick = timed ? strtoul(frame + 2, nullptr, 10) : previousTick;
    if (count && (int32_t)(tick - previousTick) < 0) {
      queue.rewind(position);
      break;
    }
    previousTick = tick;
    store.checkpoint();
    if (!store.appendRaw(frame, length)) {
      store.rollback();
      queue.rewind(position);
      break;
    }
    *lastLength = length;
    count++;
  }
  if (count) store.tailer();
  return count;
}

uint8_t replayBatchLimit(const ReplayIsolation& isolation, uint8_t linkLimit)
{
  return isolation.suspect && isolation.limit < linkLimit ? isolation.limit : linkLimit;
}

// Each POST pays a fixed cost in AT commands and HTTP round trips, so large
// batches clear a backlog faster. A failed large batch resends everything,
// so shrink quickly on failure and grow back slowly.
uint8_t adaptBatchLimit(uint8_t limit, bool sent, uint16_t status)
{
  if (sent) return limit + HTTP_BATCH_GROW_STEP < HTTP_BATCH_MAX_SAMPLES ? limit + HTTP_BATCH_GROW_STEP : HTTP_BATCH_MAX_SAMPLES;
  // HTTP 400 refuses the bytes, not the link. Isolation handles it.
  if (status == 400) return limit;
  return limit / 2 > HTTP_BATCH_MIN_SAMPLES ? limit / 2 : HTTP_BATCH_MIN_SAMPLES;
}

// Apply the collector's answer to a replayed batch. HTTP 400 is a permanent
// refusal of these bytes: split the batch, and move a refused single frame to
// the reject file on the card so it cannot block later readings.
void settleReplayBatch(DurableQueue& queue, ReplayIsolation& isolation, bool sent, uint16_t status,
                       uint8_t count, const char* frame, uint16_t length)
{
  if (sent || (status == 400 && count == 1)) {
    if (sent) queue.acknowledge();
    else if (!queue.quarantine(frame, length)) { queue.retry(); return; }
    isolation.suspect = count < isolation.suspect ? isolation.suspect - count : 0;
    if (isolation.limit > isolation.suspect) isolation.limit = isolation.suspect;
    return;
  }
  queue.retry();
  if (status != 400) return;
  Serial.println("[QUEUE] Collector refused a batch; halving it to find the refused frame");
  isolation.suspect = count;
  isolation.limit = count / 2;
}
#endif

void telemetry(void* inst)
{
  uint32_t lastRssiTime = 0;
  uint32_t lastCellRetryTime = 0;
  uint8_t connErrors = 0;
  bool wifiFallback = false;
  CStorageRAM store;
  store.init(
#if BOARD_HAS_PSRAM
    (char*)heap_caps_malloc(SERIALIZE_BUFFER_SIZE, MALLOC_CAP_SPIRAM),
#else
    (char*)malloc(SERIALIZE_BUFFER_SIZE),
#endif
    SERIALIZE_BUFFER_SIZE
  );
  teleClient.reset();
#if STORAGE == STORAGE_SD && SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
  // The last replayed frame stays here, so a refused single-frame batch can
  // be moved to the reject file without another SD read.
  static char replayFrame[SAMPLE_FRAME_SIZE];
  uint16_t replayFrameLength = 0;
  // Narrows down a batch the collector refused instead of retrying it forever.
  ReplayIsolation isolation = {0, 0};
#endif
  uint8_t batchLimit = HTTP_BATCH_MAX_SAMPLES;

  for (;;) {
    if (state.check(STATE_STANDBY)) {
      if (state.check(STATE_CELL_CONNECTED) || state.check(STATE_WIFI_CONNECTED)) {
        // Announce parked state once, then shut the radios down. There is no
        // periodic tracking traffic while the vehicle is stationary.
        if (teleClient.notify(EVENT_PING)) {
          Serial.println("[POWER] Parked state sent; tracking paused");
        }
        teleClient.shutdown();
        netop = "";
        ip = "";
        rssi = 0;
      }
      state.clear(STATE_NET_READY | STATE_CELL_CONNECTED | STATE_WIFI_CONNECTED);
      teleClient.reset();
      // Stay entirely off-network until the MEMS wake path clears standby.
      // This avoids recurring SIM traffic and makes parked power draw stable.
      telemetryParked = true;
      while (state.check(STATE_STANDBY)) delay(1000);
      telemetryParked = false;
      continue;
    }

    // A wake is not a trip until the engine or wheels show it. Keep the modem
    // off until then, so a false wake costs no network registration.
    if (powerPhase == PHASE_CONFIRMING && state.check(STATE_WORKING)) {
      delay(250);
      continue;
    }

#if ENABLE_WIFI
    if (wifiSSID[0] && (!PREFER_CELLULAR || wifiFallback) && !state.check(STATE_WIFI_CONNECTED)) {
      Serial.print("[WIFI] Joining SSID:");
      Serial.println(wifiSSID);
      teleClient.wifi.begin(wifiSSID, wifiPassword);
      teleClient.wifi.setup();
    }
#endif

    while (state.check(STATE_WORKING)) {
#if ENABLE_WIFI
      if (wifiSSID[0] && (!PREFER_CELLULAR || wifiFallback)) {
        if (!state.check(STATE_WIFI_CONNECTED) && teleClient.wifi.connected()) {
          ip = teleClient.wifi.getIP();
          if (ip.length()) {
            Serial.print("[WIFI] IP:");
            Serial.println(ip);
          }
          connErrors = 0;
          if (teleClient.connect()) {
            state.set(STATE_WIFI_CONNECTED | STATE_NET_READY);
#if !PREFER_CELLULAR
            // switch off cellular module when wifi connected
            if (state.check(STATE_CELL_CONNECTED)) {
              teleClient.cell.end();
              state.clear(STATE_CELL_CONNECTED);
              Serial.println("[CELL] Deactivated");
            }
#endif
          }
        } else if (state.check(STATE_WIFI_CONNECTED) && !teleClient.wifi.connected()) {
          Serial.println("[WIFI] Disconnected");
          state.clear(STATE_NET_READY | STATE_WIFI_CONNECTED);
          wifiFallback = false;
        }
      }
#endif
      if (!state.check(STATE_WIFI_CONNECTED) && !state.check(STATE_CELL_CONNECTED)) {
        connErrors = 0;
#if ENABLE_WIFI && PREFER_CELLULAR
        if (wifiFallback) {
          teleClient.wifi.end();
          wifiFallback = false;
        }
#endif
        if (!initCell() || !teleClient.connect()) {
          teleClient.cell.end();
          state.clear(STATE_NET_READY | STATE_CELL_CONNECTED);
          Serial.println("[CELL] Deactivated");
#if ENABLE_WIFI && PREFER_CELLULAR
          if (wifiSSID[0]) {
            wifiFallback = true;
            lastCellRetryTime = millis();
            Serial.println("[NET] Falling back to WiFi");
            break;
          }
#endif
          // avoid turning on/off cellular module too frequently to avoid operator banning
          // Standby must not wait behind this back-off; the modem is already off.
          for (uint32_t started = millis(); millis() - started < 60000UL * 3 && state.check(STATE_WORKING);) delay(1000);
          break;
        }
        Serial.println("[CELL] In service");
        state.set(STATE_NET_READY);
#if ENABLE_WIFI && PREFER_CELLULAR
        wifiFallback = false;
#endif
      }

      if (millis() - lastRssiTime > SIGNAL_CHECK_INTERVAL * 1000) {
#if ENABLE_WIFI
        if (state.check(STATE_WIFI_CONNECTED))
        {
          rssi = teleClient.wifi.RSSI();
        }
        else
#endif
        {
          rssi = teleClient.cell.RSSI();
        }
        if (rssi) {
          Serial.print("RSSI:");
          Serial.print(rssi);
          Serial.println("dBm");
        }
        lastRssiMeasurement = lastRssiTime = millis();

#if ENABLE_WIFI
        if (PREFER_CELLULAR && wifiFallback && state.check(STATE_WIFI_CONNECTED) &&
            millis() - lastCellRetryTime > 1000L * PING_BACK_INTERVAL) {
          Serial.println("[NET] Retrying preferred cellular connection");
          teleClient.shutdown();
          state.clear(STATE_NET_READY | STATE_WIFI_CONNECTED);
          wifiFallback = false;
          break;
        }
        if (wifiSSID[0] && (!PREFER_CELLULAR || wifiFallback) && !state.check(STATE_WIFI_CONNECTED)) {
          teleClient.wifi.begin(wifiSSID, wifiPassword);
        }
#endif
      }

#if GNSS == GNSS_CELLULAR
      GPS_DATA* fix = nullptr;
      if (teleClient.cell.getLocation(&fix) && fix && fix->ts) {
        portENTER_CRITICAL(&sensorMux);
        if (!gpsSnapshot.ts || fix->date != gpsSnapshot.date || fix->time != gpsSnapshot.time) gpsSnapshot = *fix;
        portEXIT_CRITICAL(&sensorMux);
      }
#endif
      // Coalesce queued samples into one POST. This keeps live latency low when
      // only one sample is waiting but avoids a request storm after an outage.
      CBuffer* batch[HTTP_BATCH_MAX_SAMPLES];
      uint8_t batchCount = 0;
      bool replaying = false;
      store.purge();
#if SERVER_PROTOCOL == PROTOCOL_UDP
      store.header(devid);
#endif
#if STORAGE == STORAGE_SD && SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
      // A sample held in RAM after an SD write failure must not starve behind
      // a journal that keeps receiving fresh samples indefinitely.
      replaying = durableQueue.pendingBytes() != 0 && bufman.recordedReadings() == 0;
      if (replaying) {
        batchCount = buildReplayBatch(durableQueue, store, replayFrame, sizeof(replayFrame),
                                      replayBatchLimit(isolation, batchLimit), &replayFrameLength);
        if (!batchCount && !durableQueue.healthy()) {
          // Keep sending RAM-backed readings when a damaged journal cannot
          // advance. The journal stays intact for recovery and health alerts.
          replaying = false;
        }
      }
      if (!replaying)
#endif
      {
      while (batchCount < batchLimit) {
        CBuffer* buffer = bufman.getOldest(true);
        if (!buffer) {
#if SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
          if (batchCount && millis() - batch[0]->timestamp < HTTP_BATCH_MAX_WAIT_MS) {
            delay(25);
            continue;
          }
#endif
          break;
        }
#if SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
        if (batchCount) store.untailer();
#endif
        store.checkpoint();
        store.timestamp(buffer->timestamp);
        buffer->serialize(store);
        store.tailer();
        if (store.overflowed()) {
          // Keep this complete sample queued and restore the last valid packet.
          store.rollback();
          bufman.restore(buffer);
          if (batchCount) store.tailer();
          break;
        }
        batch[batchCount++] = buffer;
#if SERVER_PROTOCOL != PROTOCOL_HTTPS_POST
        break;
#endif
      }
      }
      if (!batchCount) {
        store.purge();
        delay(50);
        continue;
      }
      Serial.print("[UPLOAD] Sending ");
      Serial.print(batchCount);
      Serial.print(" readings | payload: ");
      Serial.print(store.length());
      if (replaying) {
#if STORAGE == STORAGE_SD
        Serial.print(" bytes | SD backlog: ");
        Serial.print(durableQueue.pendingBytes());
#endif
      } else {
        Serial.print(" bytes | oldest reading: ");
        Serial.print(millis() - batch[0]->timestamp);
        Serial.print(" ms");
      }
      Serial.print(" | transport: ");
#if SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
      Serial.println(teleClient.usingWifi() ? "Wi-Fi" : "cellular");
#else
      Serial.println(state.check(STATE_CELL_CONNECTED) ? "cellular" : "Wi-Fi");
#endif

#if ENABLE_NETWORK_STATUS_SIGNALS
      telemetryTransmitActive = true;
#endif
      const bool sent = teleClient.transmit(store.buffer(), store.length());
#if ENABLE_NETWORK_STATUS_SIGNALS
      telemetryTransmitActive = false;
#endif
      batchLimit = adaptBatchLimit(batchLimit, sent, teleClient.lastStatus);
#if STORAGE == STORAGE_SD && SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
      if (replaying) {
        // HTTP 400 is a permanent refusal, not an outage. It must not count
        // towards a modem reconnect or block every later reading.
        const bool refused = !sent && teleClient.lastStatus == 400;
        settleReplayBatch(durableQueue, isolation, sent, teleClient.lastStatus, batchCount,
                          replayFrame, replayFrameLength);
        if (refused) { store.purge(); continue; }
      }
#endif
      if (sent) {
        // Free the entire batch only after the server accepts it.
#if STORAGE == STORAGE_SD && SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
        if (!replaying)
#endif
          for (uint8_t i = 0; i < batchCount; i++) bufman.free(batch[i]);
        connErrors = 0;
        showStats();
      } else {
        // Retain the whole batch for an at-least-once ordered retry after the
        // connection recovers instead of silently dropping outage data.
#if STORAGE == STORAGE_SD && SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
        if (!replaying)
#endif
          for (uint8_t i = 0; i < batchCount; i++) {
            bufman.restore(batch[i]);
          }
        timeoutsNet++;
        connErrors++;
        printTimeoutStats();
        if (connErrors < MAX_CONN_ERRORS_RECONNECT) {
          // quick reconnect
          teleClient.connect(true);
        }
      }
      store.purge();

      teleClient.inbound();

      if (state.check(STATE_CELL_CONNECTED) && !teleClient.cell.check(1000)) {
        Serial.println("[CELL] Not in service");
        state.clear(STATE_NET_READY | STATE_CELL_CONNECTED);
        break;
      }

      if (syncInterval > 10000 && millis() - teleClient.lastSyncTime > syncInterval) {
        Serial.println("[NET] Poor connection");
        timeoutsNet++;
        if (!teleClient.connect()) {
          connErrors++;
        }
      }

      if (connErrors >= MAX_CONN_ERRORS_RECONNECT) {
#if ENABLE_WIFI
        if (state.check(STATE_WIFI_CONNECTED)) {
          teleClient.wifi.end();
          state.clear(STATE_NET_READY | STATE_WIFI_CONNECTED);
          break;
        }
#endif
        if (state.check(STATE_CELL_CONNECTED)) {
          teleClient.cell.end();
          state.clear(STATE_NET_READY | STATE_CELL_CONNECTED);
          break;
        }
      }

      if (deviceTemp >= COOLING_DOWN_TEMP) {
        // device too hot, cool down by pause transmission
        Serial.print("HIGH DEVICE TEMP: ");
        Serial.println(deviceTemp);
        // A thermal pause must never erase the pending readings.
        delay(1000);
      }

    }
  }
}

/*******************************************************************************
  Implementing stand-by mode
*******************************************************************************/
void standby()
{
  state.set(STATE_STANDBY);
  state.clear(STATE_WORKING);
  xSemaphoreTake(coprocessorMutex, portMAX_DELAY);
#if STORAGE != STORAGE_NONE
  if (state.check(STATE_STORAGE_READY)) {
    logger.end();
  }
#endif

#if !GNSS_ALWAYS_ON && GNSS == GNSS_STANDALONE
  if (state.check(STATE_GPS_READY)) {
    Serial.println("[GNSS] Receiver off for standby");
    sys.gpsEnd(true);
    state.clear(STATE_GPS_READY | STATE_GPS_ONLINE);
    gd = 0;
  }
#endif

  state.clear(STATE_WORKING | STATE_OBD_READY | STATE_STORAGE_READY);
  // The telemetry task owns the modem UART. Let it send the parked marker and
  // power the modem down before light sleep stops the UART mid-command and
  // leaves the modem on for the whole park.
  for (uint32_t started = millis(); !telemetryParked && millis() - started < STANDBY_RADIO_OFF_WAIT_MS;) delay(100);
  if (!telemetryParked) Serial.println("[POWER] Modem shutdown not confirmed; entering standby anyway");
  // this will put co-processor into sleep mode
#if ENABLE_OLED
  oled.print("STANDBY");
  delay(1000);
  oled.clear();
#endif
  Serial.println("[POWER] Standby: radios off; tracking paused until serious motion");
  obd.enterLowPowerMode();
#if ENABLE_MEMS
  if (mems && state.check(STATE_MEMS_READY)) {
    calibrateMEMS();
    mems->setLowPower(true);
  }
  waitMotion(-1, STANDBY_MOTION_THRESHOLD, STANDBY_MOTION_CONFIRM_SAMPLES);
#elif ENABLE_OBD
  do {
    delay(5000);
  } while (readVehicleVoltage() < JUMPSTART_VOLTAGE);
#else
  delay(5000);
#endif
  Serial.println("[POWER] Restarting active mode");
  sys.resetLink();
#if RESET_AFTER_WAKEUP
  // Light sleep keeps RAM. Reboot only when no reading exists solely in RAM;
  // otherwise resume in place so the reading can still be journaled or sent.
  if (!bufman.unpersistedReadings()) {
#if ENABLE_MEMS
    if (mems) mems->end();
#endif
    ESP.restart();
  }
  Serial.println("[POWER] RAM-only readings held; resuming without reboot");
  bootWakeReason = (wakeRecord & 0xFFFFFF00UL) == WAKE_MAGIC ? (uint8_t)(wakeRecord & 0xFF) : WAKE_POWER_ON;
  wakeRecord = 0;
#endif
  state.clear(STATE_STANDBY);
  xSemaphoreGive(coprocessorMutex);
}

/*******************************************************************************
  Tasks to perform in idle/waiting time
*******************************************************************************/
void genDeviceID(char* buf)
{
    uint64_t seed = ESP.getEfuseMac() >> 8;
    for (int i = 0; i < 8; i++, seed >>= 5) {
      byte x = (byte)seed & 0x1f;
      if (x >= 10) {
        x = x - 10 + 'A';
        switch (x) {
          case 'B': x = 'W'; break;
          case 'D': x = 'X'; break;
          case 'I': x = 'Y'; break;
          case 'O': x = 'Z'; break;
        }
      } else {
        x += '0';
      }
      buf[i] = x;
    }
    buf[8] = 0;
}

void showSysInfo()
{
  Serial.println();
  Serial.println("[BOOT] Freematics TeleLogger starting");
  Serial.print("[BOOT] Build: ");
  Serial.println(FREEMATICS_BUILD_ID);
  Serial.print("[BOOT] Release: ");
  Serial.println(FREEMATICS_RELEASE);
  Serial.print("[BOOT] CPU: ");
  Serial.print(ESP.getCpuFreqMHz());
  Serial.print(" MHz | flash: ");
  Serial.print(ESP.getFlashChipSize() >> 20);
  Serial.println(" MB");
  Serial.print("[BOOT] RAM: ");
  Serial.print(ESP.getHeapSize() >> 10);
  Serial.print(" KB");
#if BOARD_HAS_PSRAM
  if (psramInit()) {
    Serial.print(" | PSRAM: ");
    Serial.print(esp_spiram_get_size() >> 20);
    Serial.print(" MB");
  }
#endif
  Serial.println();

  int rtc = rtc_clk_slow_freq_get();
  if (rtc) {
    Serial.print("[BOOT] RTC source: ");
    Serial.println(rtc);
  }

#if ENABLE_OLED
  oled.clear();
  oled.print("CPU:");
  oled.print(ESP.getCpuFreqMHz());
  oled.print("Mhz ");
  oled.print(getFlashSize() >> 10);
  oled.println("MB Flash");
#endif

  Serial.print("[BOOT] Device ID: ");
  Serial.println(devid);
#if ENABLE_OLED
  oled.print("DEVICE ID:");
  oled.println(devid);
#endif
}

void loadConfig()
{
  size_t len;
  len = sizeof(apn);
  apn[0] = 0;
  nvs_get_str(nvs, "CELL_APN", apn, &len);
  if (!apn[0]) {
    strcpy(apn, CELL_APN);
  }

#if ENABLE_WIFI
  len = sizeof(wifiSSID);
  nvs_get_str(nvs, "WIFI_SSID", wifiSSID, &len);
  len = sizeof(wifiPassword);
  nvs_get_str(nvs, "WIFI_PWD", wifiPassword, &len);
#endif
}

void processBLE(int timeout)
{
#if ENABLE_BLE
  static byte echo = 0;
  char* cmd;
  if (!(cmd = ble_recv_command(timeout))) {
    return;
  }

  char *p = strchr(cmd, '\r');
  if (p) *p = 0;
  char buf[48];
  int bufsize = sizeof(buf);
  int n = 0;
  if (echo) n += snprintf(buf + n, bufsize - n, "%s\r", cmd);
  Serial.print("[BLE] ");
  Serial.print(cmd);
  if (!strcmp(cmd, "UPTIME") || !strcmp(cmd, "TICK")) {
    n += snprintf(buf + n, bufsize - n, "%lu", millis());
  } else if (!strcmp(cmd, "BATT")) {
    n += snprintf(buf + n, bufsize - n, "%.2f", (float)(analogRead(A0) * 42) / 4095);
  } else if (!strcmp(cmd, "RESET")) {
#if STORAGE
    logger.end();
#endif
    ESP.restart();
    // never reach here
  } else if (!strcmp(cmd, "OFF")) {
    state.set(STATE_STANDBY);
    state.clear(STATE_WORKING);
    n += snprintf(buf + n, bufsize - n, "OK");
  } else if (!strcmp(cmd, "ON")) {
    state.clear(STATE_STANDBY);
    n += snprintf(buf + n, bufsize - n, "OK");
  } else if (!strcmp(cmd, "ON?")) {
    n += snprintf(buf + n, bufsize - n, "%u", state.check(STATE_STANDBY) ? 0 : 1);
  } else if (!strcmp(cmd, "APN?")) {
    n += snprintf(buf + n, bufsize - n, "%s", *apn ? apn : "DEFAULT");
  } else if (!strncmp(cmd, "APN=", 4)) {
    n += snprintf(buf + n, bufsize - n, persistConfigString("CELL_APN", strcmp(cmd + 4, "DEFAULT") ? cmd + 4 : "") ? "OK" : "ERR");
    loadConfig();
  } else if (!strcmp(cmd, "NET_OP")) {
    if (state.check(STATE_WIFI_CONNECTED)) {
#if ENABLE_WIFI
      n += snprintf(buf + n, bufsize - n, "%s", wifiSSID[0] ? wifiSSID : "-");
#endif
    } else {
      snprintf(buf + n, bufsize - n, "%s", netop.length() ? netop.c_str() : "-");
      char *p = strchr(buf + n, ' ');
      if (p) *p = 0;
      n += strlen(buf + n);
    }
  } else if (!strcmp(cmd, "NET_IP")) {
    n += snprintf(buf + n, bufsize - n, "%s", ip.length() ? ip.c_str() : "-");
  } else if (!strcmp(cmd, "NET_PACKET")) {
      n += snprintf(buf + n, bufsize - n, "%u", teleClient.txCount);
  } else if (!strcmp(cmd, "NET_DATA")) {
      n += snprintf(buf + n, bufsize - n, "%u", teleClient.txBytes);
  } else if (!strcmp(cmd, "NET_RATE")) {
      n += snprintf(buf + n, bufsize - n, "%u", teleClient.startTime ? (unsigned int)((uint64_t)(teleClient.txBytes + teleClient.rxBytes) * 3600 / (millis() - teleClient.startTime)) : 0);
  } else if (!strcmp(cmd, "RSSI")) {
    n += snprintf(buf + n, bufsize - n, "%d", rssi);
#if ENABLE_WIFI
  } else if (!strcmp(cmd, "SSID?")) {
    n += snprintf(buf + n, bufsize - n, "%s", wifiSSID[0] ? wifiSSID : "-");
  } else if (!strncmp(cmd, "SSID=", 5)) {
    const char* p = cmd + 5;
    n += snprintf(buf + n, bufsize - n, persistConfigString("WIFI_SSID", strcmp(p, "-") ? p : "") ? "OK" : "ERR");
    loadConfig();
  } else if (!strcmp(cmd, "WPWD?")) {
    n += snprintf(buf + n, bufsize - n, "%s", wifiPassword[0] ? wifiPassword : "-");
  } else if (!strncmp(cmd, "WPWD=", 5)) {
    const char* p = cmd + 5;
    n += snprintf(buf + n, bufsize - n, persistConfigString("WIFI_PWD", strcmp(p, "-") ? p : "") ? "OK" : "ERR");
    loadConfig();
#else
  } else if (!strcmp(cmd, "SSID?") || !strcmp(cmd, "WPWD?")) {
    n += snprintf(buf + n, bufsize - n, "-");
#endif
#if ENABLE_MEMS
  } else if (!strcmp(cmd, "TEMP")) {
    n += snprintf(buf + n, bufsize - n, "%d", (int)deviceTemp);
  } else if (!strcmp(cmd, "ACC")) {
    n += snprintf(buf + n, bufsize - n, "%.1f/%.1f/%.1f", acc[0], acc[1], acc[2]);
  } else if (!strcmp(cmd, "GYRO")) {
    n += snprintf(buf + n, bufsize - n, "%.1f/%.1f/%.1f", gyr[0], gyr[1], gyr[2]);
  } else if (!strcmp(cmd, "GF")) {
    n += snprintf(buf + n, bufsize - n, "%f", (float)sqrt(acc[0]*acc[0] + acc[1]*acc[1] + acc[2]*acc[2]));
#endif
  } else if (!strcmp(cmd, "ATE0")) {
    echo = 0;
    n += snprintf(buf + n, bufsize - n, "OK");
  } else if (!strcmp(cmd, "ATE1")) {
    echo = 1;
    n += snprintf(buf + n, bufsize - n, "OK");
  } else if (!strcmp(cmd, "FS")) {
    n += snprintf(buf + n, bufsize - n, "%u",
#if STORAGE == STORAGE_NONE
    0
#else
    logger.size()
#endif
      );
  } else if (!memcmp(cmd, "01", 2)) {
    byte pid = hex2uint8(cmd + 2);
    OBDSnapshot snapshot;
    portENTER_CRITICAL(&sensorMux);
    snapshot = obdSnapshot;
    portEXIT_CRITICAL(&sensorMux);
    for (byte i = 0; i < sizeof(obdData) / sizeof(obdData[0]); i++) {
      if (snapshot.readings[i].pid == pid && snapshot.readings[i].ts) {
        n += snprintf(buf + n, bufsize - n, "%.2f", snapshot.readings[i].value);
        pid = 0;
        break;
      }
    }
    if (pid) n += snprintf(buf + n, bufsize - n, "N/A");
  } else if (!strcmp(cmd, "VIN")) {
    n += snprintf(buf + n, bufsize - n, "%s", vin[0] ? vin : "N/A");
  } else if (!strcmp(cmd, "LAT") && gd) {
    n += snprintf(buf + n, bufsize - n, "%f", gd->lat);
  } else if (!strcmp(cmd, "LNG") && gd) {
    n += snprintf(buf + n, bufsize - n, "%f", gd->lng);
  } else if (!strcmp(cmd, "ALT") && gd) {
    n += snprintf(buf + n, bufsize - n, "%d", (int)gd->alt);
  } else if (!strcmp(cmd, "SAT") && gd) {
    n += snprintf(buf + n, bufsize - n, "%u", (unsigned int)gd->sat);
  } else if (!strcmp(cmd, "SPD") && gd) {
    n += snprintf(buf + n, bufsize - n, "%d", (int)(gd->speed * 1852 / 1000));
  } else if (!strcmp(cmd, "CRS") && gd) {
    n += snprintf(buf + n, bufsize - n, "%u", (unsigned int)gd->heading);
  } else {
    n += snprintf(buf + n, bufsize - n, "ERROR");
  }
  Serial.print(" -> ");
  Serial.println((p = strchr(buf, '\r')) ? p + 1 : buf);
  if (n < bufsize - 1) {
    buf[n++] = '\r';
  } else {
    n = bufsize - 1;
  }
  buf[n] = 0;
  ble_send_response(buf, n, cmd);
#else
  if (timeout) delay(timeout);
#endif
}

void setup()
{
  // The sampler runs in this Arduino loop task. On 30 September a 250 ms slot
  // was missed while the upload task (priority 2) compressed and read the SD
  // card. The sampler does a few milliseconds of work per slot and then
  // sleeps, so it takes the highest priority and nothing can delay a reading.
  vTaskPrioritySet(nullptr, SAMPLER_TASK_PRIORITY);
  delay(500);

  // Initialize NVS
  esp_err_t err = nvs_flash_init();
  if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    // NVS partition was truncated and needs to be erased
    // Retry nvs_flash_init
    ESP_ERROR_CHECK(nvs_flash_erase());
    err = nvs_flash_init();
  }
  ESP_ERROR_CHECK( err );
  err = nvs_open("storage", NVS_READWRITE, &nvs);
  if (err == ESP_OK) {
    loadConfig();
  }

#if ENABLE_OLED
  oled.begin();
  oled.setFontSize(FONT_SIZE_SMALL);
#endif
  // initialize USB serial
  Serial.begin(115200);

  // init LED pin
#ifdef PIN_LED
  pinMode(PIN_LED, OUTPUT);
  if (ledMode == 0) digitalWrite(PIN_LED, HIGH);
#endif

  uint32_t savedUTC = 0;
  if (nvs_get_u32(nvs, "last_utc", &savedUTC) == ESP_OK && savedUTC >= 1704067200UL) {
    struct timeval tv = {(time_t)savedUTC, 0};
    settimeofday(&tv, nullptr);
    Serial.println("[TIME] Restored last known UTC; cellular/GNSS can correct it");
  }

  // generate unique device ID
  genDeviceID(devid);
  bootWakeReason = (wakeRecord & 0xFFFFFF00UL) == WAKE_MAGIC ? (uint8_t)(wakeRecord & 0xFF) : WAKE_POWER_ON;
  wakeRecord = 0;

#if CONFIG_MODE_TIMEOUT
  configMode();
#endif

#if LOG_EXT_SENSORS == 1
  pinMode(PIN_SENSOR1, INPUT);
  pinMode(PIN_SENSOR2, INPUT);
#elif LOG_EXT_SENSORS == 2
  adc1_config_width(ADC_WIDTH_BIT_12);
  adc1_config_channel_atten(ADC1_CHANNEL_0, ADC_ATTEN_DB_11);
  adc1_config_channel_atten(ADC1_CHANNEL_1, ADC_ATTEN_DB_11);
#endif

  // show system information
  showSysInfo();

  bufman.init();
  
  //Serial.print(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) >> 10);
  //Serial.println("KB");

#if ENABLE_OBD
  if (sys.begin()) {
    Serial.print("[BOOT] Hardware type: ");
    Serial.println(sys.devType);
    obd.begin(sys.link);
  }
#else
  sys.begin(false, true);
#endif

#if ENABLE_MEMS
if (!state.check(STATE_MEMS_READY)) do {
  Serial.print("[MEMS] Motion sensor: ");
  mems = new ICM_42627;
  byte ret = mems->begin();
  if (ret) {
    state.set(STATE_MEMS_READY);
    Serial.println("ICM-42627");
    break;
  }
  delete mems;
  mems = new ICM_20948_I2C;
  ret = mems->begin();
  if (ret) {
    state.set(STATE_MEMS_READY);
    hasMagnetometer = true;
    Serial.println("ICM-20948");
    break;
  } 
  delete mems;
  /*
  mems = new MPU9250;
  ret = mems->begin();
  if (ret) {
    state.set(STATE_MEMS_READY);
    Serial.println("MPU-9250");
    break;
  }
  */
  mems = 0;
  Serial.println("NO");
} while (0);
#endif

#if ENABLE_HTTPD
  IPAddress ip;
  if (serverSetup(ip)) {
    Serial.println("HTTPD:");
    Serial.println(ip);
#if ENABLE_OLED
    oled.println(ip);
#endif
  } else {
    Serial.println("HTTPD:NO");
  }
#endif

  state.set(STATE_WORKING);

#if ENABLE_BLE
  // init BLE
  ble_init("FreematicsPlus");
#endif

  // initialize components
#if ENABLE_NETWORK_STATUS_SIGNALS || STORAGE == STORAGE_SD
  // Start the alarm before GNSS, OBD and storage setup can block.
  if (!statusTask.create(statusSignals, "status", 1, 3072)) {
    Serial.println("[CRITICAL] Status task creation failed");
  }
#endif
  coprocessorMutex = xSemaphoreCreateMutex();
  memsMutex = xSemaphoreCreateMutex();
  if (!coprocessorMutex || !memsMutex) ESP.restart();
#if ENABLE_OBD
  clearOBDReadings();
  publishOBDSnapshot();
#endif
  initialize();
  if (!coprocessorMutex ||
      !recorderTask.create(recordSamples, "recorder", 1, 8192) ||
      !obdTask.create(acquireOBD, "obd", 1, 8192) ||
      !gpsTask.create(acquireGPS, "gnss", 1, 4096) ||
      !memsTask.create(acquireMEMS, "mems", 1, 4096)) {
    Serial.println("[CRITICAL] Acquisition/recorder task creation failed");
    ESP.restart();
  }

  // initialize network and maintain connection
  if (!subtask.create(telemetry, "telemetry", 2, 24576)) {
    Serial.println("[CRITICAL] Upload task creation failed; retaining local recordings");
  }
#if !ENABLE_NETWORK_STATUS_SIGNALS
#ifdef PIN_LED
  digitalWrite(PIN_LED, LOW);
#endif
#endif
}

void loop()
{
  // error handling
  if (!state.check(STATE_WORKING)) {
    standby();
#if !ENABLE_NETWORK_STATUS_SIGNALS && defined(PIN_LED)
    if (ledMode == 0) digitalWrite(PIN_LED, HIGH);
#endif
    initialize();
#if !ENABLE_NETWORK_STATUS_SIGNALS && defined(PIN_LED)
    digitalWrite(PIN_LED, LOW);
#endif
    return;
  }

  process();
}
