/*************************************************************************
* Arduino Library for Freematics ONE+
* Distributed under BSD license
* Visit https://freematics.com for more information
* (C)2012-2019 Developed by Stanley Huang <stanley@freematics.com.au>
*************************************************************************/

#include <Arduino.h>
#include "FreematicsBase.h"
#include "FreematicsOBD.h"
#include "../../obd_raw_mode01.h"
#include "utility/OBDPidScaling.h"
#include "mode02_response.h"

int dumpLine(char* buffer, int len)
{
	int bytesToDump = len >> 1;
	for (int i = 0; i < len; i++) {
		// find out first line end and discard the first line
		if (buffer[i] == '\r' || buffer[i] == '\n') {
			// go through all following \r or \n if any
			while (++i < len && (buffer[i] == '\r' || buffer[i] == '\n'));
			bytesToDump = i;
			break;
		}
	}
	memmove(buffer, buffer + bytesToDump, len - bytesToDump);
	return bytesToDump;
}

uint16_t hex2uint16(const char *p)
{
	char c = *p;
	uint16_t i = 0;
	for (uint8_t n = 0; c && n < 4; c = *(++p)) {
		if (c >= 'A' && c <= 'F') {
			c -= 7;
		} else if (c>='a' && c<='f') {
			c -= 39;
        } else if (c == ' ' && n == 2) {
            continue;
        } else if (c < '0' || c > '9') {
			break;
        }
		i = (i << 4) | (c & 0xF);
		n++;
	}
	return i;
}

byte hex2uint8(const char *p)
{
	byte c1 = *p;
	byte c2 = *(p + 1);
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

/*************************************************************************
* OBD-II UART Bridge
*************************************************************************/

bool COBD::readPID(byte pid, int& result)
{
	float value;
	if (!readPID(pid, value)) return false;
	result = (int)value;
	return true;
}

// Validate every byte consumed by normalizeData before publishing a reading.
static byte decodedPIDBytes(byte pid)
{
    if ((pid >= PID_O2_S1_WR_VOLTAGE && pid <= PID_O2_S8_WR_VOLTAGE) ||
        (pid >= PID_O2_S1_WR_CURRENT && pid <= PID_O2_S8_WR_CURRENT) || pid == PID_ODOMETER) return 4;
    switch (pid) {
    case PID_RPM:
    case PID_MAF_FLOW:
    case PID_FREEZE_DTC:
    case PID_DISTANCE:
    case PID_DISTANCE_WITH_MIL:
    case PID_TIME_WITH_MIL:
    case PID_TIME_SINCE_CODES_CLEARED:
    case PID_RUNTIME:
    case PID_FUEL_RAIL_PRESSURE:
    case PID_ENGINE_REF_TORQUE:
    case PID_FUEL_RAIL_PRESSURE_RELATIVE:
    case PID_FUEL_RAIL_PRESSURE_DIRECT:
    case PID_ABS_EVAP_SYS_VAPOR_PRESSURE:
    case PID_EVAP_SYS_VAPOR_PRESSURE:
    case PID_EVAP_SYS_VAPOR_PRESSURE_ALT:
    case PID_CONTROL_MODULE_VOLTAGE:
    case PID_ENGINE_FUEL_RATE:
    case PID_FUEL_INJECTION_TIMING:
    case PID_CATALYST_TEMP_B1S1:
    case PID_CATALYST_TEMP_B2S1:
    case PID_CATALYST_TEMP_B1S2:
    case PID_CATALYST_TEMP_B2S2:
    case PID_AIR_FUEL_EQUIV_RATIO:
        return 2;
    default:
        return 1;
    }
}

bool COBD::readPID(byte pid, float& result)
{
	byte ignoredLength = 0;
	return readPID(pid, result, nullptr, 0, ignoredLength);
}

bool COBD::readPID(byte pid, float& result, byte* rawBytes, byte rawCapacity, byte& rawLength)
{
	if (rawBytes && rawCapacity) memset(rawBytes, 0, rawCapacity);
	rawLength = 0;
	const byte decodedRequired = decodedPIDBytes(pid);
	const byte catalogRawRequired = rawBytes ? freematics::obd_raw::expectedBytes(pid) : 0;
	const byte rawRequired = catalogRawRequired > decodedRequired ?
		catalogRawRequired : (rawBytes ? decodedRequired : 0);
	if (rawRequired && rawCapacity < rawRequired) return false;
	char buffer[64] = {};
	char* data = 0;
	sprintf(buffer, "%02X%02X\r", dataMode, pid);
    if (!link || !link->send(buffer)) {
        if (errors < 255) errors++;
        return false;
    }
	idleTasks();
	int ret = link->receive(buffer, sizeof(buffer), OBD_TIMEOUT_SHORT);
    if (ret > 0) buffer[ret < (int)sizeof(buffer) ? ret : sizeof(buffer) - 1] = 0;
	if (ret > 0 && !checkErrorMessage(buffer)) {
		char *p = buffer;
		while ((p = strstr(p, "41 "))) {
			p += 3;
			byte curpid = hex2uint8(p);
			if (strlen(p) >= 3 && isxdigit((unsigned char)p[0]) && isxdigit((unsigned char)p[1]) &&
                p[2] == ' ' && curpid == pid) {
				while (*p && *p != ' ') p++;
				while (*p == ' ') p++;
				if (*p) {
					data = p;
					break;
				}
			}
		}
	}

	char validated[12] = {};
	const byte required = decodedRequired;
	bool valid = data != nullptr;
	const byte parsedBytes = rawRequired > required ? rawRequired : required;
	byte parsed[4] = {};
	if (valid) valid = freematics::obd_raw::parseBytes(data, parsedBytes, parsed);
	for (byte index = 0; valid && index < parsedBytes; index++) {
		if (index < required) {
			snprintf(validated + index * 3, sizeof(validated) - index * 3, "%02X", parsed[index]);
			if (index) validated[index * 3 - 1] = ' ';
		}
		data += 2;
	}
    if (!valid) {
        if (errors < 255) errors++;
        return false;
    }
	result = normalizeData(pid, validated);
	if (rawBytes && rawRequired && rawCapacity >= rawRequired) {
		memcpy(rawBytes, parsed, rawRequired);
		rawLength = rawRequired;
	}
	errors = 0;
	return true;
}

bool COBD::readFreezeFramePID(byte pid, float& result, uint32_t timeout)
{
	char command[12];
	char response[128] = {};
	char normalized[16] = {};
	snprintf(command, sizeof(command), "02%02X00\r", pid);
	if (!link || !link->send(command)) {
		if (errors < 255) errors++;
		return false;
	}
	idleTasks();
	int length = link->receive(response, sizeof(response) - 1, timeout);
	if (length <= 0) {
		if (errors < 255) errors++;
		return false;
	}
	response[length < (int)sizeof(response) ? length : sizeof(response) - 1] = 0;
	if (checkErrorMessage(response) ||
		!freematics::mode02::parseFrame0(response, pid, decodedPIDBytes(pid),
			normalized, sizeof(normalized))) {
		if (errors < 255) errors++;
		return false;
	}
	result = normalizeData(pid, normalized);
	errors = 0;
	return true;
}

byte COBD::readPID(const byte pid[], byte count, int result[])
{
	byte results = 0;
	for (byte n = 0; n < count; n++) {
		if (readPID(pid[n], result[n])) {
			results++;
		}
	}
	return results;
}

static bool readDTCByte(const char*& cursor, byte& value)
{
	while (*cursor) {
		if ((*cursor >= '0' && *cursor <= '9')
			|| (*cursor >= 'A' && *cursor <= 'F')
			|| (*cursor >= 'a' && *cursor <= 'f')) {
			const char* next = cursor + 1;
			if ((*next >= '0' && *next <= '9')
				|| (*next >= 'A' && *next <= 'F')
				|| (*next >= 'a' && *next <= 'f')) {
				if (next[1] == ':') {
					cursor = next + 2;
					continue;
				}
				value = hex2uint8(cursor);
				cursor += 2;
				return true;
			}
		}
		cursor++;
	}
	return false;
}

static void appendDTCByte(byte value, uint16_t codes[], byte maxCodes, int& codesRead,
	byte& highByte, bool& haveHighByte, bool& complete)
{
	if (complete || !codes || codesRead >= maxCodes) return;
	if (!haveHighByte) {
		highByte = value;
		haveHighByte = true;
		return;
	}
	uint16_t code = ((uint16_t)highByte << 8) | value;
	haveHighByte = false;
	if (code == 0) {
		complete = true;
	} else {
		codes[codesRead++] = code;
	}
}

int COBD::readDTC(uint16_t codes[], byte maxCodes)
{
	return readDTC(0x03, codes, maxCodes);
}

int COBD::readDTC(byte mode, uint16_t codes[], byte maxCodes)
{
	/*
	Response example:
	0: 43 04 01 08 01 09
	1: 01 11 01 15 00 00 00
	*/
	m_dtcStatus = DTC_STATUS_NO_RESPONSE;
	int codesRead = 0;
	if (!link || !codes || maxCodes == 0 || (mode != 0x03 && mode != 0x07 && mode != 0x0A)) return 0;
	const byte expected = 0x40 + mode;
	bool positive = false;
	bool complete = false;
	byte highByte = 0;
	bool haveHighByte = false;
	for (byte request = 0; request < OBD_DTC_MAX_RESPONSE_LINES && !complete && codesRead < maxCodes; request++) {
		char buffer[128] = {0};
		if (request == 0) {
			sprintf(buffer, "%02X\r", mode);
		} else {
			sprintf(buffer, "%02X%02X\r", mode, request);
		}
		if (!link->send(buffer)) break;
		if (link->receive(buffer, sizeof(buffer), OBD_DTC_TIMEOUT) <= 0 || checkErrorMessage(buffer)) break;

		const char* cursor = buffer;
		byte token = 0;
		if (!positive) {
			while (readDTCByte(cursor, token) && token != expected);
			if (token != expected) break;
			positive = true;
			byte responseLength = 0;
			if (!readDTCByte(cursor, responseLength)) break;
			m_dtcStatus = DTC_STATUS_RESPONSE;
			for (byte index = 0; index < responseLength && !complete && readDTCByte(cursor, token); index++) {
				appendDTCByte(token, codes, maxCodes, codesRead, highByte, haveHighByte, complete);
			}
		} else {
			while (!complete && codesRead < maxCodes && readDTCByte(cursor, token)) {
				appendDTCByte(token, codes, maxCodes, codesRead, highByte, haveHighByte, complete);
			}
		}
	}
	if (codesRead) m_dtcStatus = DTC_STATUS_CODES;
	return codesRead;
}

void COBD::clearDTC()
{
	char buffer[32];
	link->send("04\r");
	link->receive(buffer, sizeof(buffer), OBD_TIMEOUT_LONG);
}

float COBD::normalizeData(byte pid, char* data)
{
	float result;
	switch (pid) {
	case PID_RPM:
		result = getLargeValue(data) / 4.0f;
		break;
	case PID_FUEL_PRESSURE: // kPa
		result = getSmallValue(data) * 3.0f;
		break;
	case PID_COOLANT_TEMP:
	case PID_INTAKE_TEMP:
	case PID_AMBIENT_TEMP:
	case PID_ENGINE_OIL_TEMP:
		result = getTemperatureValue(data);
		break;
	case PID_THROTTLE:
	case PID_COMMANDED_EGR:
	case PID_COMMANDED_EVAPORATIVE_PURGE:
	case PID_FUEL_LEVEL:
	case PID_RELATIVE_THROTTLE_POS:
	case PID_ABSOLUTE_THROTTLE_POS_B:
	case PID_ABSOLUTE_THROTTLE_POS_C:
	case PID_ACC_PEDAL_POS_D:
	case PID_ACC_PEDAL_POS_E:
	case PID_ACC_PEDAL_POS_F:
	case PID_COMMANDED_THROTTLE_ACTUATOR:
	case PID_ENGINE_LOAD:
	case PID_ABSOLUTE_ENGINE_LOAD:
	case PID_ETHANOL_FUEL:
	case PID_HYBRID_BATTERY_PERCENTAGE:
	case PID_ACCELERATOR_POS_RELATIVE:
		result = getSmallValue(data) * 100.0f / 255.0f;
		break;
	case PID_O2_B1S1_VOLTAGE:
	case PID_O2_B1S2_VOLTAGE:
	case PID_O2_B1S3_VOLTAGE:
	case PID_O2_B1S4_VOLTAGE:
	case PID_O2_B2S1_VOLTAGE:
	case PID_O2_B2S2_VOLTAGE:
	case PID_O2_B2S3_VOLTAGE:
	case PID_O2_B2S4_VOLTAGE:
		result = getSmallValue(data) / 200.0f;
		break;
	case PID_MAF_FLOW: // grams/sec
		result = getLargeValue(data) / 100.0f;
		break;
	case PID_TIMING_ADVANCE:
		result = getSmallValue(data) / 2.0f - 64.0f;
		break;
	case PID_FREEZE_DTC:
	case PID_DISTANCE: // km
	case PID_DISTANCE_WITH_MIL: // km
	case PID_TIME_WITH_MIL: // minute
	case PID_TIME_SINCE_CODES_CLEARED: // minute
	case PID_RUNTIME: // second
	case PID_ENGINE_REF_TORQUE: // Nm
		result = getLargeValue(data);
		break;
	case PID_FUEL_RAIL_PRESSURE: // 10 kPa per bit
		result = freematics::obd::absoluteFuelRailPressureKpa(getLargeValue(data));
		break;
	case PID_FUEL_RAIL_PRESSURE_RELATIVE:
		result = (int16_t)getLargeValue(data) / 4.0f;
		break;
	case PID_FUEL_RAIL_PRESSURE_DIRECT:
	case PID_ABS_EVAP_SYS_VAPOR_PRESSURE:
		result = getLargeValue(data) / 200.0f;
		break;
	case PID_EVAP_SYS_VAPOR_PRESSURE:
		result = (int16_t)getLargeValue(data) / 4.0f;
		break;
	case PID_EVAP_SYS_VAPOR_PRESSURE_ALT:
		result = getLargeValue(data) - 32767.0f;
		break;
	case PID_O2_S1_WR_VOLTAGE:
	case PID_O2_S2_WR_VOLTAGE:
	case PID_O2_S3_WR_VOLTAGE:
	case PID_O2_S4_WR_VOLTAGE:
	case PID_O2_S5_WR_VOLTAGE:
	case PID_O2_S6_WR_VOLTAGE:
	case PID_O2_S7_WR_VOLTAGE:
	case PID_O2_S8_WR_VOLTAGE:
		result = getLargeValue(data + 6) * 8.0f / 65536.0f;
		break;
	case PID_O2_S1_WR_CURRENT:
	case PID_O2_S2_WR_CURRENT:
	case PID_O2_S3_WR_CURRENT:
	case PID_O2_S4_WR_CURRENT:
	case PID_O2_S5_WR_CURRENT:
	case PID_O2_S6_WR_CURRENT:
	case PID_O2_S7_WR_CURRENT:
	case PID_O2_S8_WR_CURRENT:
		result = getLargeValue(data + 6) / 256.0f - 128.0f;
		break;
	case PID_CONTROL_MODULE_VOLTAGE: // V
		result = getLargeValue(data) / 1000.0f;
		break;
	case PID_ENGINE_FUEL_RATE: // L/h
		result = getLargeValue(data) / 20.0f;
		break;
	case PID_ENGINE_TORQUE_DEMANDED: // %
	case PID_ENGINE_TORQUE_PERCENTAGE: // %
		result = (int)getSmallValue(data) - 125;
		break;
	case PID_SHORT_TERM_FUEL_TRIM_1:
	case PID_LONG_TERM_FUEL_TRIM_1:
	case PID_SHORT_TERM_FUEL_TRIM_2:
	case PID_LONG_TERM_FUEL_TRIM_2:
	case PID_EGR_ERROR:
		result = ((int)getSmallValue(data) - 128) * 100.0f / 128.0f;
		break;
	case PID_FUEL_INJECTION_TIMING:
		result = getLargeValue(data) / 128.0f - 210.0f;
		break;
	case PID_CATALYST_TEMP_B1S1:
	case PID_CATALYST_TEMP_B2S1:
	case PID_CATALYST_TEMP_B1S2:
	case PID_CATALYST_TEMP_B2S2:
		result = getLargeValue(data) / 10.0f - 40.0f;
		break;
	case PID_AIR_FUEL_EQUIV_RATIO:
		result = getLargeValue(data) / 32768.0f;
		break;
	case PID_ODOMETER: {
		if (strlen(data) < 11)
			result = -1;
		else
			result = ((uint32_t)hex2uint8(data) << 24 | (uint32_t)hex2uint8(data + 3) << 16 | (uint32_t)hex2uint8(data + 6) << 8 | hex2uint8(data + 9)) / 10.0f;
		break;
	}
	default:
		result = getSmallValue(data);
	}
	return result;
}

char* COBD::getResponse(byte& pid, char* buffer, byte bufsize)
{
	if (!link) return 0;
	while (link->receive(buffer, bufsize, OBD_TIMEOUT_SHORT) > 0) {
		char *p = buffer;
		while ((p = strstr(p, "41 "))) {
		    p += 3;
		    byte curpid = hex2uint8(p);
		    if (pid == 0) pid = curpid;
		    if (curpid == pid) {
		        errors = 0;
		        p += 2;
		        if (*p == ' ')
		            return p + 1;
		    }
		}
	}
	return 0;
}

void COBD::enterLowPowerMode()
{
  	char buf[32];
	if (link) {
		reset();
		delay(1000);	
		link->sendCommand("ATLP\r", buf, sizeof(buf), 1000);
	}
}


void COBD::leaveLowPowerMode()
{
	// send any command to wake up
	char buf[32];
	if (!link) return;
	for (byte n = 0; n < 30 && !link->sendCommand("ATI\r", buf, sizeof(buf), 1000); n++);
}

char* COBD::getResultValue(char* buf)
{
	char* p = buf;
	for (;;) {
		if (isdigit(*p) || *p == '-') {
			return p;
		}
		p = strchr(p, '\r');
		if (!p) break;
		if (*(++p) == '\n') p++;
	}
	return 0;
}

float COBD::getVoltage()
{
    char buf[32];
	if (link && link->sendCommand("ATRV\r", buf, sizeof(buf), 500) > 0) {
		char* p = getResultValue(buf);
		if (p) return (float)atof(p);
    }
    return 0;
}

bool COBD::getVIN(char* buffer, byte bufsize)
{
	if (!link || !buffer || bufsize < 5) return false;
	for (byte n = 0; n < 2; n++) {
		if (link->sendCommand("0902\r", buffer, bufsize, OBD_TIMEOUT_LONG)) {
			char vin[18];
			char hex[34];
			uint16_t hexLength = 0;
			bool overflow = false;
			bool started = false;
			const char* line = buffer;
			while (*line) {
				const char* end = line;
				while (*end && *end != '\r' && *end != '\n' && *end != '>') end++;
				const bool prompt = *end == '>';

				const char* data = line;
				while (data < end && (*data == ' ' || *data == '\t')) data++;
				if (data < end && data + 1 < end && data[0] >= '0' && data[0] <= '9' && data[1] == ':') {
					data += 2;
				}
				while (data < end && (*data == ' ' || *data == '\t')) data++;

				// ELM multi-frame responses may start with a byte-count line (e.g. "014").
				if (end - data == 3 && data[0] >= '0' && data[0] <= '9' &&
					data[1] >= '0' && data[1] <= '9' && data[2] >= '0' && data[2] <= '9') {
					line = end;
					while (*line == '\r' || *line == '\n') line++;
					continue;
				}

				char compact[256];
				uint16_t compactLength = 0;
				bool validLine = true;
				for (const char* p = data; p < end; p++) {
					if (*p == ' ' || *p == '\t') continue;
					if (!((*p >= '0' && *p <= '9') || (*p >= 'A' && *p <= 'F') || (*p >= 'a' && *p <= 'f')) ||
						compactLength >= sizeof(compact) - 1) {
						validLine = false;
						break;
					}
					compact[compactLength++] = *p;
				}

				if (validLine && compactLength > 0) {
					byte offset = 0;
					if (compactLength >= 6 && compact[0] == '4' && compact[1] == '9' &&
						compact[2] == '0' && compact[3] == '2') {
						offset = 6; // Mode 09 PID 02 and its message sequence/count byte.
						started = true;
					}
					if (started) {
						for (uint16_t i = offset; i < compactLength; i++) {
							if (hexLength >= sizeof(hex)) {
								overflow = true;
								break;
							}
							hex[hexLength++] = compact[i];
						}
					}
				}

				if (prompt) break;
				line = end;
				while (*line == '\r' || *line == '\n') line++;
			}

			if (!overflow && hexLength == sizeof(hex)) {
				for (byte i = 0; i < sizeof(vin) - 1; i++) {
					byte hi = hex[i * 2] >= 'a' ? hex[i * 2] - 'a' + 10 :
						hex[i * 2] >= 'A' ? hex[i * 2] - 'A' + 10 : hex[i * 2] - '0';
					byte lo = hex[i * 2 + 1] >= 'a' ? hex[i * 2 + 1] - 'a' + 10 :
						hex[i * 2 + 1] >= 'A' ? hex[i * 2 + 1] - 'A' + 10 : hex[i * 2 + 1] - '0';
					char c = (char)((hi << 4) | lo);
					if (c >= 'a' && c <= 'z') c -= 'a' - 'A';
					if (!((c >= 'A' && c <= 'Z' && c != 'I' && c != 'O' && c != 'Q') ||
						(c >= '0' && c <= '9'))) {
						hexLength = 0;
						break;
					}
					vin[i] = c;
				}
				if (hexLength == sizeof(hex)) {
					vin[17] = 0;
					memcpy(buffer, vin, sizeof(vin));
					return true;
				}
			}
		}
		delay(100);
	}
    return false;
}

bool COBD::readReadOnlyService(byte service, uint16_t identifier, char* buffer, byte bufsize,
	unsigned int timeout)
{
	if (!link || m_state != OBD_CONNECTED || !buffer || bufsize < 8) return false;
	if (service != 0x09 && service != 0x1A && service != 0x22) return false;
	if (service != 0x22 && identifier > 0xFF) return false;

	char command[16];
	char expected[16];
	if (service == 0x22) {
		snprintf(command, sizeof(command), "%02X%04X\r", service, identifier);
		snprintf(expected, sizeof(expected), "%02X %02X %02X",
			(byte)(service + 0x40), (byte)(identifier >> 8), (byte)identifier);
	}
	else {
		snprintf(command, sizeof(command), "%02X%02X\r", service, (byte)identifier);
		snprintf(expected, sizeof(expected), "%02X %02X",
			(byte)(service + 0x40), (byte)identifier);
	}
	if (!link->sendCommand(command, buffer, bufsize, timeout)) return false;
	if (service == 0x09) {
		char compact[8];
		snprintf(compact, sizeof(compact), "%02X%02X",
			(byte)(service + 0x40), (byte)identifier);
		return strstr(buffer, expected) != 0 || strstr(buffer, compact) != 0;
	}
	return strstr(buffer, expected) != 0;
}

bool COBD::isValidPID(byte pid)
{
	if (pid == 0) return false;
	uint16_t index = (uint16_t)pid - 1;
	if (index >= sizeof(pidmap) * 8) return false;
	byte i = index >> 3;
	byte b = 0x80 >> (index & 0x7);
	return (pidmap[i] & b) != 0;
}

bool COBD::init(OBD_PROTOCOLS protocol, bool quick)
{
	const char *initcmd[] = {"ATE0\r", "ATH0\r"};
	char buffer[64];
	bool success = false;

	if (!link) {
		return false;
	}

	memset(pidmap, 0, sizeof(pidmap));
	m_protocol = 0;
	m_state = OBD_DISCONNECTED;
	for (byte n = 0; n < 3; n++) {
		if (link->sendCommand("ATZ\r", buffer, sizeof(buffer), OBD_TIMEOUT_SHORT)) {
			success = true;
			break;
		}
	}
	if (!success) return false;
	for (byte i = 0; i < sizeof(initcmd) / sizeof(initcmd[0]); i++) {
		link->sendCommand(initcmd[i], buffer, sizeof(buffer), OBD_TIMEOUT_SHORT);
	}
	if (protocol != PROTO_AUTO) {
		sprintf(buffer, "ATSP %X\r", protocol);
		if (!link->sendCommand(buffer, buffer, sizeof(buffer), OBD_TIMEOUT_SHORT) || !strstr(buffer, "OK")) {
			return false;
		}
		m_protocol = (byte)protocol;
	}
	if (protocol == PROTO_J1939) {
		m_protocol = (byte)protocol;
		m_state = OBD_CONNECTED;
		errors = 0;
		return true;
	}

	success = false;
	if (quick) {
		int value;
		success = readPID(PID_SPEED, value);
		if (!success) return false;
	} else {
		for (byte n = 0; n < 2; n++) {
			int value;
			if (readPID(PID_SPEED, value)) {
				success = true;
				break;
			}
		}
		if (!success) return false;
	}

	/* ATDPN is read-only and records the bridge's auto-selected protocol. */
	if (m_protocol == 0) {
		char protocolBuffer[32];
		if (link->sendCommand("ATDPN\r", protocolBuffer, sizeof(protocolBuffer), OBD_TIMEOUT_SHORT) > 0) {
			char* protocolValue = getResultValue(protocolBuffer);
			if (protocolValue) {
				int selected = atoi(protocolValue);
				if (selected > 0 && selected <= 0xFF) m_protocol = (byte)selected;
			}
		}
	}

	for (byte i = 0; i < 8; i++) {
		byte pid = i * 0x20;
		sprintf(buffer, "%02X%02X\r", dataMode, pid);
		link->send(buffer);
		if (!link->receive(buffer, sizeof(buffer), OBD_TIMEOUT_SHORT) || checkErrorMessage(buffer)) {
			continue;
		}
		for (char *p = buffer; (p = strstr(p, "41 ")); ) {
			p += 3;
			if (hex2uint8(p) == pid) {
				p += 2;
				for (byte n = 0; n < 4 && *(p + n * 3) == ' '; n++) {
					pidmap[i * 4 + n] = hex2uint8(p + n * 3 + 1);
				}
				success = true;
			}
		}
	}

	if (success) {
		m_state = OBD_CONNECTED;
		errors = 0;
	}
	return success;
}

void COBD::reset()
{
	char buf[32];
	if (link) link->sendCommand("ATR\r", buf, sizeof(buf), OBD_TIMEOUT_SHORT);
}

void COBD::uninit()
{
	char buf[32];
	if (link) link->sendCommand("ATPC\r", buf, sizeof(buf), OBD_TIMEOUT_SHORT);
}

byte COBD::checkErrorMessage(const char* buffer)
{
	const char *errmsg[] = {"UNABLE", "ERROR", "TIMEOUT", "NO DATA"};
	for (byte i = 0; i < sizeof(errmsg) / sizeof(errmsg[0]); i++) {
		if (strstr(buffer, errmsg[i])) return i + 1;
	}
	return 0;
}

uint8_t COBD::getPercentageValue(char* data)
{
  return (uint16_t)hex2uint8(data) * 100 / 255;
}

uint16_t COBD::getLargeValue(char* data)
{
  return hex2uint16(data);
}

uint8_t COBD::getSmallValue(char* data)
{
  return hex2uint8(data);
}

int16_t COBD::getTemperatureValue(char* data)
{
  return (int)hex2uint8(data) - 40;
}

void COBD::setHeaderID(uint32_t num)
{
	if (link) {
		char buf[32];
		sprintf(buf, "ATSH %X\r", num & 0xffffff);
		link->sendCommand(buf, buf, sizeof(buf), 1000);
		sprintf(buf, "ATCP %X\r", num & 0x1f);
		link->sendCommand(buf, buf, sizeof(buf), 1000);
	}
}

void COBD::sniff(bool enabled)
{
	if (link) {
		char buf[32];
		link->sendCommand(enabled ? "ATM1\r" : "ATM0\r", buf, sizeof(buf), 1000);
	}
}

void COBD::setHeaderFilter(uint32_t num)
{
	if (link) {
		char buf[32];
		sprintf(buf, "ATCF %X\r", num);
		link->sendCommand(buf, buf, sizeof(buf), 1000);
	}
}
	
void COBD::setHeaderMask(uint32_t bitmask)
{
	if (link) {
		char buf[32];
		sprintf(buf, "ATCM %X\r", bitmask);
		link->sendCommand(buf, buf, sizeof(buf), 1000);
	}
}

int COBD::receiveData(byte* buf, int len)
{
	if (!link) return 0;
	int n = 0;
	for (n = 0; n < len; ) {
		int c = link->read();
		if (c == -1 || c == '\r') break;
		buf[n++] = c;
	}
	if (n == 0) return 0;
	int bytes = 0;
	len = n;
	if (buf[0] == '$') {
		for (n = 1; n < len && buf[n] != ','; n++);
		for (; n < len && buf[n] == ','; bytes++) {
			byte d = hex2uint8((const char*)buf + n + 1);
			n += 3;
			if (buf[n] != ',' && buf[n] != '\r') {
				if (d != hex2uint8((const char*)buf + n)) break;
				n += 2;
			}
			buf[bytes] = d;
		}
	} else {
		for (n = 0; n < len; bytes++) {
			buf[bytes] = hex2uint8((const char*)buf + n);
			n += 2;
			if (buf[n++] != ' ') break;
		}
	}
	return bytes;
}

int COBD::receiveRawData(char* buf, int len, unsigned int timeout)
{
	if (!link || !buf || len < 2) return 0;
	int bytes = link->receive(buf, len, timeout);
	if (bytes < 0) return 0;
	buf[bytes < len ? bytes : len - 1] = 0;
	return bytes;
}

void COBD::setCANID(uint16_t id)
{
	if (link) {
		char buf[32];
		sprintf(buf, "ATSH %X\r", id);
		link->sendCommand(buf, buf, sizeof(buf), 1000);
	}
}

int COBD::sendCANMessage(byte msg[], int len, char* buf, int bufsize)
{
	if (!link) return 0;
	char cmd[258];
	if (len * 2 >= sizeof(cmd) - 1) len = sizeof(cmd) / 2 - 2; 
	for (int n = 0; n < len; n++) {
		sprintf(cmd + n * 2, "%02X", msg[n]); 
	}
	cmd[len * 2] = '\r';
	cmd[len * 2 + 1] = 0;
	return link->sendCommand(cmd, buf, bufsize, 100);
}
