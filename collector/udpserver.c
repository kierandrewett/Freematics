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
#include <limits.h>
#include <stdlib.h>
#include <sys/stat.h>
#include "httpd.h"
#include "teleserver.h"

CHANNEL_DATA* assignChannel(const char* id);
FILE* createDataFile(CHANNEL_DATA* pld);
void clearLiveData(CHANNEL_DATA* pld);

extern char serverKey[];

//////////////////////////////////////////////////////////////////////////
// callback from the web server whenever it recevies UDP data
//////////////////////////////////////////////////////////////////////////

static int parseUnsignedField(const char* value, unsigned long max, unsigned long* result)
{
	if (!value || !*value) return 0;
	for (const unsigned char* p = (const unsigned char*)value; *p; p++) if (!isdigit(*p)) return 0;
	errno = 0;
	char* end = NULL;
	unsigned long parsed = strtoul(value, &end, 10);
	if (end == value || *end || errno == ERANGE || parsed > max) return 0;
	if (result) *result = parsed;
	return 1;
}

static int parseSignedField(const char* value, long min, long max, long* result)
{
	if (!value || !*value) return 0;
	const char* p = value;
	if (*p == '+' || *p == '-') p++;
	if (!*p) return 0;
	for (const unsigned char* q = (const unsigned char*)p; *q; q++) if (!isdigit(*q)) return 0;
	errno = 0;
	char* end = NULL;
	long parsed = strtol(value, &end, 10);
	if (end == value || *end || errno == ERANGE || parsed < min || parsed > max) return 0;
	if (result) *result = parsed;
	return 1;
}

static int validDeviceID(const char* id)
{
	if (!id) return 0;
	size_t len = strlen(id);
	if (len < MIN_DEVID_LEN || len >= sizeof(((CHANNEL_DATA*)0)->devid)) return 0;
	for (const unsigned char* p = (const unsigned char*)id; *p; p++) if (!isalnum(*p)) return 0;
	return 1;
}

static int authorizedPeer(const CHANNEL_DATA* pld, const struct sockaddr_in* peer)
{
	return pld && peer && pld->udpPeer.sin_family == peer->sin_family
		&& pld->udpPeer.sin_port == peer->sin_port
		&& pld->udpPeer.sin_addr.s_addr == peer->sin_addr.s_addr;
}

int verifyChecksum(char* data)
{
	if (!data) return 0;
	uint8_t sum = 0;
	char *p = strrchr(data, '*');
	if (!p || p == data || !isxdigit((unsigned char)p[1]) || (p[2] && (!isxdigit((unsigned char)p[2]) || p[3]))) return 0;
	for (char *s = data; s < p; s++) sum += (uint8_t)*s;
	if (hex2uint8(p + 1) == sum) {
		*p = 0; // strip checksum
		return 1;
	}
	return 0;
}
int addChecksump(char* data)
{
	uint8_t sum = 0;
	char *s;
	for (s = data; *s; s++) sum += *s;
	s += sprintf(s, "*%X", sum);
	return (int)(s - data);
}

int incomingUDPCallback(void* _hp)
{
	HttpParam* hp = (HttpParam*)_hp;
	struct sockaddr_in cliaddr;
	socklen_t socklen = sizeof(cliaddr);
	char buf[4096];
	int recv;
	char* hostaddr;

	if ((recv = recvfrom(hp->udpSocket, buf, sizeof(buf) - 1, 0, (struct sockaddr *)&cliaddr, &socklen)) <= 0)
		return -1;


	/*
	Data format:
	<ID>#<timestamp>:<pid>=<data>[$<checksum>]
	*/

	buf[recv] = 0;
	hostaddr = inet_ntoa(cliaddr.sin_addr);
	fprintf(stderr, "%u bytes from %s | ", recv, hostaddr);

	// validate checksum
	if (!verifyChecksum(buf)) {
		fprintf(stderr, "UDP data checksum mismatch\n%s\n", buf);
		return -1;
	}

	CHANNEL_DATA* pld = 0;
	char *msg = 0;
	char *data;
	char* devid = 0;

	// validate header
	data = strchr(buf, '#');
	if (!data) {
		// invalid header
		fprintf(stderr, "Invalid data received - %s\n", buf);
		return -1;
	}

	// parse feed ID or device ID
	*data = 0;
	if ((int)(data - buf) > 4) {
		devid = buf;
		pld = findChannelByDeviceID(buf);
		if (pld) {
			devid = buf;
		}
	}
	else {
		int id = hex2uint16(buf);
		if (id) pld = findChannelByID(id);
	}
	data++; // now points to the start of data chunks

	uint64_t serverTick = GetTickCount64();
	uint32_t deviceTick = 0;
	uint32_t token = 0;
	uint16_t eventID = 0;
	uint16_t devflags = 0;
	int rssi = 0;

	char* vin = 0;
	char* key = 0;
	int hasEvent = 0;
	int hasToken = 0;
	int hasMessage = 0;
	int hasDeviceID = 0;
	if (strstr(data, "EV=")) {
	char *s = strtok(data, ",");
	while (s) {
		if (!strncmp(s, "EV=", 3)) {
			unsigned long parsed;
			if (hasEvent || !parseUnsignedField(s + 3, EVENT_PING, &parsed)) return -1;
			eventID = (uint16_t)parsed;
			hasEvent = 1;
		}
		else if (!strncmp(s, "TS=", 3)) {
			unsigned long parsed;
			if (!parseUnsignedField(s + 3, UINT32_MAX, &parsed)) return -1;
			deviceTick = (uint32_t)parsed;
		}
		else if (!strncmp(s, "TK=", 3)) {
			unsigned long parsed;
			if (hasToken || !parseUnsignedField(s + 3, UINT32_MAX, &parsed) || parsed == 0) return -1;
			token = (uint32_t)parsed;
			hasToken = 1;
		}
		else if (!strncmp(s, "MSG=", 4)) {
			if (hasMessage || strlen(s + 4) >= MAX_COMMAND_MSG_LEN) return -1;
			msg = s + 4;
			hasMessage = 1;
		}
		else if (!strncmp(s, "ID=", 3)) {
			if (hasDeviceID || !validDeviceID(s + 3)) return -1;
			hasDeviceID = 1;
			devid = s + 3;
		}
		else if (!strncmp(s, "VIN=", 4)) {
			if (vin) return -1;
			vin = s + 4;
		}
		else if (!strncmp(s, "DF=", 3)) {
			unsigned long parsed;
			if (!parseUnsignedField(s + 3, UINT16_MAX, &parsed)) return -1;
			devflags = (uint16_t)parsed;
		}
		else if (!strncmp(s, "SSI=", 4)) {
			long parsed;
			if (!parseSignedField(s + 4, INT16_MIN, INT16_MAX, &parsed)) return -1;
			rssi = (int)parsed;
		}
		else if (!strncmp(s, "SK=", 3)) {
			if (key || strlen(s + 3) >= 256) return -1;
			key = s + 3;
		}
		s = strtok(0, ",");
	}
	}

	if (hasEvent && eventID == EVENT_ACK && (!hasToken || !hasMessage)) return -1;
	if (hasEvent && eventID == EVENT_LOGIN) {
		if (!devid) devid = vin;
		if (!validDeviceID(devid)) return -1;
		/* Check credentials before allocating or mutating a channel. */
		if (*serverKey && (!key || strcmp(serverKey, key))) return -2;
		CHANNEL_DATA* existing = findChannelByDeviceID(devid);
		if (!*serverKey && existing && existing->udpPeer.sin_family
			&& !authorizedPeer(existing, &cliaddr)) return -2;
		pld = assignChannel(devid);
		if (!pld) {
			fprintf(getLogFile(), "No more channel");
			return 0;
		}

		if (vin && checkVIN(vin)) {
			strcpy(pld->vin, vin);
		}
		pld->rssi = rssi;
		pld->devflags = devflags;
		// TODO: also check timed out device
		if (*serverKey) {
			// match server key
			if (key && !strcmp(serverKey, key)) {
				memcpy(&pld->udpPeer, &cliaddr, sizeof(cliaddr));
			}
			else {
				return -2;
			}
		}
		else {
			// always accept
			memcpy(&pld->udpPeer, &cliaddr, sizeof(cliaddr));
		}
		int deviceRestarted = deviceTick && pld->deviceTick
			&& deviceTick < pld->deviceTick
			&& pld->deviceTick - deviceTick > PROXY_MAX_TIME_BEHIND;
		if (!pld->fp || deviceRestarted) {
			if (pld->fp) {
				fclose(pld->fp);
				pld->fp = 0;
			}
			clearLiveData(pld);
			pld->serverDataTick = serverTick;
			pld->sessionStartTick = serverTick;
			deviceLogin(pld);
		}
		else {
			/* Re-login after transport loss keeps the open archive trip. */
			pld->flags |= FLAG_RUNNING;
			pld->flags &= ~(FLAG_SLEEPING | FLAG_PINGED);
			pld->serverDataTick = serverTick;
			printf("DEVICE RE-LOGIN, ID:%s\n", pld->devid);
		}
		if (deviceTick) pld->deviceTick = deviceTick;
	}
	if (!pld) {
		fprintf(stderr, "INVALID CHANNEL - %s\n", buf);
		return -1;
	}

	/* Login establishes the peer; every other packet must come from it. */
	if (eventID != EVENT_LOGIN && !authorizedPeer(pld, &cliaddr)) {
		fprintf(stderr, "Unauthorized peer\n");
		return -1;
	}

	pld->dataReceived += recv;

	if (eventID == 0 || eventID == EVENT_PING) {
		processPayload(data, pld, eventID);
	} else if (eventID == EVENT_ACK) {
		// pending command executed
		for (int i = 0; i < MAX_PENDING_COMMANDS; i++) {
			COMMAND_BLOCK *cmd = pld->cmd + i;
			if (cmd->token && cmd->token == token) {
				size_t len = strlen(msg);
				char* message = malloc(len + 1);
				if (!message) return -1;
				memcpy(message, msg, len + 1);
				free(cmd->message);
				cmd->message = message;
				cmd->len = (uint8_t)len;
				cmd->flags |= CMD_FLAG_RESPONDED;
				cmd->elapsed = (uint16_t)(pld->serverDataTick - cmd->tick);
				break;
			}
		}
		// no response needed for ACK
		return 0;
	}

	if (eventID == 0) {
		if (serverTick - pld->serverSyncTick >= SYNC_INTERVAL * 1000) {
			// send sync event
			pld->serverSyncTick = serverTick;
			eventID = EVENT_SYNC;
		}
		else {
			// no response if no sync is required
			return 0;
		}
	}
	// generate response
	int len = sprintf(buf, "%X#EV=%u,RX=%u,TX=%u", pld->id, eventID, pld->recvCount, ++pld->txCount);
	switch (eventID) {
	case EVENT_LOGOUT:
		deviceLogout(pld);
		break;
	case EVENT_PING:
		fprintf(stderr, "Ping received\n");
		pld->serverPingTick = serverTick;
		pld->flags &= ~FLAG_RUNNING;
		pld->flags |= (FLAG_SLEEPING | FLAG_PINGED);
		break;
	case EVENT_RECONNECT:
		/* Reconnect is transport liveness; keep the open archive trip. */
		pld->flags |= FLAG_RUNNING;
		pld->flags &= ~(FLAG_SLEEPING | FLAG_PINGED);
		pld->serverDataTick = serverTick;
		fprintf(stderr, "DEVICE RECONNECTED, ID:%s\n", pld->devid);
		break;
	}
	// send UDP response
	len = addChecksump(buf);
	if (sendto(hp->udpSocket, buf, len, 0, (struct sockaddr *)&cliaddr, socklen) == len)
		fprintf(stderr, "Reply sent:%s\n", buf);
	else
		fprintf(stderr, "Reply unsent\n");

	return 0;
}

uint32_t issueCommand(HttpParam* hp, CHANNEL_DATA* pld, const char* cmd, uint32_t token)
{
	if (!hp || !pld || !isReadOnlyCommand(cmd)) return 0;
	if (token == 0) token = ++pld->cmdCount;
	char buf[128];
	int written = snprintf(buf, sizeof(buf), "%X#EV=%u,TK=%u,CMD=%s", pld->id, EVENT_COMMAND, token, cmd);
	if (written < 0 || written >= (int)sizeof(buf) - 3) return 0;
	int len = addChecksump(buf);
	socklen_t socklen = sizeof(struct sockaddr);
	pld->serverDataTick = GetTickCount64();
	if (sendto(hp->udpSocket, buf, (size_t)len, 0, (struct sockaddr*)&pld->udpPeer, socklen) == len) {
		fprintf(stderr, "Command sent: %s (%u)\n", cmd, token);
		COMMAND_BLOCK* pending = 0;
		for (int i = 0; i < MAX_PENDING_COMMANDS; i++) {
			pending = pld->cmd + i;
			if (pending->token == 0 || (pending->flags & CMD_FLAG_CHECKED)) break;
			pending = 0;
		}
		if (!pending) {
			unsigned int maxElapsed = 0;
			for (int i = 0; i < MAX_PENDING_COMMANDS; i++) {
				unsigned int elapsed = (unsigned int)(pld->serverDataTick - pld->cmd[i].tick);
				if (elapsed >= maxElapsed) {
					pending = pld->cmd + i;
					maxElapsed = elapsed;
				}
			}
		}
		if (pending) {
			pending->token = token;
			pending->tick = pld->serverDataTick;
			pending->flags = 0;
		}
		return token;
	}
	fprintf(stderr, "Command unsent\n");
	return 0;
}
