/*************************************************************************
* Base class for Freematics telematics products
* Distributed under BSD license
* Visit https://freematics.com for more information
* (C)2017-2018 Stanley Huang <stanley@freematics.com.au
*************************************************************************/

#ifndef FREEMATICS_BASE
#define FREEMATICS_BASE

#include <Arduino.h>

// The RTC value restored from NVS is only a hint until synchronized during
// this boot. Capture timestamps must not claim UTC validity before then.
bool freematicsSystemTimeTrusted();
void freematicsMarkSystemTimeTrusted();

// non-OBD/custom PIDs (no mode number)
#define PID_GPS_LATITUDE 0xA
#define PID_GPS_LONGITUDE 0xB
#define PID_GPS_ALTITUDE 0xC
#define PID_GPS_SPEED 0xD
#define PID_GPS_HEADING 0xE
#define PID_GPS_SAT_COUNT 0xF
#define PID_GPS_TIME 0x10
#define PID_GPS_DATE 0x11
#define PID_GPS_HDOP 0x12
#define PID_ACC 0x20
#define PID_GYRO 0x21
#define PID_COMPASS 0x22
#define PID_BATTERY_VOLTAGE 0x24
#define PID_ORIENTATION 0x25

// custom PIDs
#define PID_TIMESTAMP 0
#define PID_TRIP_DISTANCE 0x30
#define PID_DATA_SIZE 0x80
#define PID_CSQ 0x81
#define PID_DEVICE_TEMP 0x82
#define PID_DEVICE_HALL 0x83
#define PID_NETWORK_TRANSPORT 0x84
#define PID_DTC_STORED_COUNT 0x300
#define PID_DTC_STORED_BASE 0x301
#define PID_DTC_PENDING_COUNT 0x320
#define PID_DTC_PENDING_BASE 0x321
#define PID_DTC_PERMANENT_COUNT 0x340
#define PID_DTC_PERMANENT_BASE 0x341
#define PID_DTC_STORED_STATUS 0x310
#define PID_DTC_PENDING_STATUS 0x330
#define PID_DTC_PERMANENT_STATUS 0x350
#define PID_OBD_PROTOCOL 0x85
#define PID_OBD_SUPPORTED_PIDS 0x86
#define PID_OBD_TIMEOUTS 0x87
#define PID_OBD_LAST_LATENCY 0x88
#define PID_OBD_STATE 0x89
#define PID_OBD_FAST_FAILURES 0x8A
#define PID_QUEUE_READINGS 0x8B
#define PID_QUEUE_BYTES 0x8C
#define PID_DURABLE_QUEUE_BYTES 0x8D
#define PID_MISSED_READINGS 0x8E
#define PID_DURABLE_QUEUE_HEALTH 0x8F
#define PID_CAPTURE_UTC_SECONDS 0x90
#define PID_CAPTURE_UTC_MILLISECONDS 0x91
#define PID_CAN_FRAME 0x92
#define PID_GPS_AGE 0x93
#define PID_VOLTAGE_AGE 0x94
#define PID_MEMS_AGE 0x95
#define PID_CSQ_AGE 0x96
#define PID_REJECTED_READINGS 0x97
#define PID_POWER_PHASE 0x98
#define PID_WAKE_REASON 0x99
#define PID_ACC_PEAK 0x9A
#define PID_ACC_PEAK_VECTOR 0x9B
#define PID_VOLTAGE_MIN 0x9C
#define PID_VOLTAGE_MAX 0x9D
// Condition-monitoring waveform format 1. Repeated fields retain raw sensor
// acquisitions in source order inside an ordinary telemetry frame.
#define PID_WAVEFORM_VOLTAGE 0xA0
#define PID_WAVEFORM_MOTION_TIMESTAMP 0xA1
#define PID_WAVEFORM_RAW_ACCELERATION 0xA2
#define PID_WAVEFORM_GYRO 0xA3
#define PID_WAVEFORM_LOSSES 0xA4
#define PID_WAVEFORM_FORMAT 0xA5
#define PID_OBD_AGE_BASE 0x400
#define PID_DTC_AGE_BASE 0x360
#define DTC_CODE_SLOTS 15
#define PID_EXT_SENSOR1 0x90
#define PID_EXT_SENSOR2 0x91

typedef struct {
	float pitch;
	float yaw;
	float roll;
} ORIENTATION;

typedef struct {
	uint32_t ts;
	uint32_t date;
	uint32_t time;
	float lat;
	float lng;
	float alt; /* meter */
	float speed; /* knot */
	uint16_t heading; /* degree */
	uint8_t hdop;
	uint8_t sat;
	uint16_t sentences;
	uint16_t errors;
} GPS_DATA;

class CLink
{
public:
	virtual ~CLink() {}
	virtual bool begin(unsigned int baudrate = 0, int rxPin = 0, int txPin = 0) { return true; }
	virtual void end() {}
	// send command and receive response
	virtual int sendCommand(const char* cmd, char* buf, int bufsize, unsigned int timeout) { return 0; }
	// receive data from SPI
	virtual int receive(char* buffer, int bufsize, unsigned int timeout) { return 0; }
	// write data to SPI
	virtual bool send(const char* str) { return false; }
	virtual int read() { return -1; }
};

class CFreematics
{
public:
	typedef bool (*ContinueCheck)(void* context);
	virtual void begin() {}
	// start xBee UART communication
	virtual bool xbBegin(unsigned long baudrate = 115200L, int pinRx = 0, int pinTx = 0) = 0;
	virtual void xbEnd() {}
	// read data to xBee UART
	virtual int xbRead(char* buffer, int bufsize, unsigned int timeout = 1000) = 0;
	// send data to xBee UART
	virtual void xbWrite(const char* cmd) = 0;
  // send data to xBee UART
	virtual void xbWrite(const char* data, int len) = 0;
	// receive data from xBee UART (returns 0/1/2)
	virtual int xbReceive(char* buffer, int bufsize, unsigned int timeout = 1000, const char** expected = 0, byte expectedCount = 0) = 0;
	// Additive cancellable receive; legacy device implementations remain compatible.
	virtual int xbReceiveCancellable(char* buffer, int bufsize, unsigned int timeout,
		const char** expected, byte expectedCount, ContinueCheck continueCheck, void* context) {
		if (continueCheck && !continueCheck(context)) return -2;
		int result = xbReceive(buffer, bufsize, timeout, expected, expectedCount);
		return continueCheck && !continueCheck(context) ? -2 : result;
	}
	// purge xBee UART buffer
	virtual void xbPurge() = 0;
	// toggle xBee module power
	virtual void xbTogglePower(unsigned int duration = 500) = 0;
};

#endif
