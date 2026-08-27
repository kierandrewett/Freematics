/******************************************************************************
* Freematics Hub Server - Trip APIs
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
#include <stdarg.h>
#include "cJSON.h"
#include "cdecode.h"
#include "httpd.h"
#include "teleserver.h"
#include "logdata.h"
#include "data2kml.h"

extern CHANNEL_DATA ld[];

int loadConfig();
char* getUserByDeviceID(const char* devid);
int getUserInfo(const char* username, char** ppassword, char* pdevid[], int maxdev);

#define MAX_UPLOAD_SIZE 256 * 1024
#define ARCHIVE_PATH_SIZE 512
#define TRIP_ID_LENGTH (sizeof(((CHANNEL_DATA*)0)->tripid) - 1)
#define DEVICE_ID_MAX_LENGTH (sizeof(((CHANNEL_DATA*)0)->devid) - 1)

char fileid[17];
int error = 0;
const char* errmsg[] = {"Invalid data format", "File creation error"};

FILE* fpDest;
char* xsl;
extern char dataDir[];


static int daysInMonth(int year, int month)
{
	if (month == 2) return 28 + ((year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) ? 1 : 0);
	return month == 4 || month == 6 || month == 9 || month == 11 ? 30 : 31;
}

static int isValidDeviceID(const char* devid)
{
	if (!devid) return 0;
	for (size_t i = 0; i <= DEVICE_ID_MAX_LENGTH; i++) {
		unsigned char c = (unsigned char)devid[i];
		if (!c) return i >= MIN_DEVID_LEN;
		if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9'))) return 0;
	}
	return 0;
}

static int isValidTripID(const char* tripid)
{
	if (!tripid) return 0;
	for (size_t i = 0; i < TRIP_ID_LENGTH; i++) {
		unsigned char c = (unsigned char)tripid[i];
		if (i == 8) {
			if (c != '-') return 0;
		}
		else if (c < '0' || c > '9') {
			return 0;
		}
	}
	if (tripid[TRIP_ID_LENGTH] != 0) return 0;

	int year = (tripid[0] - '0') * 1000 + (tripid[1] - '0') * 100
		+ (tripid[2] - '0') * 10 + tripid[3] - '0';
	int month = (tripid[4] - '0') * 10 + tripid[5] - '0';
	int day = (tripid[6] - '0') * 10 + tripid[7] - '0';
	int hour = (tripid[9] - '0') * 10 + tripid[10] - '0';
	int minute = (tripid[11] - '0') * 10 + tripid[12] - '0';
	int second = (tripid[13] - '0') * 10 + tripid[14] - '0';
	return year >= 1970 && month >= 1 && month <= 12
		&& day >= 1 && day <= daysInMonth(year, month)
		&& hour <= 23 && minute <= 59 && second <= 59;
}

static int buildTripPath(char* file, size_t fileSize, const char* devid, const char* tripid)
{
	if (!file || !fileSize || !isValidDeviceID(devid) || !isValidTripID(tripid)) return -1;
	int len = snprintf(file, fileSize, "%s/%.*s/%.*s/%.*s/%s",
		devid, 4, tripid, 2, tripid + 4, 2, tripid + 6, tripid);
	return len < 0 || (size_t)len >= fileSize ? -1 : 0;
}

static int buildArchivePath(char* path, size_t pathSize, const char* file, const char* extension)
{
	if (!path || !pathSize || !file || !extension) return -1;
	int len = snprintf(path, pathSize, "%s/%s%s", dataDir, file, extension);
	return len < 0 || (size_t)len >= pathSize ? -1 : 0;
}

static int writeArchiveError(UrlHandlerParam* param, int statusCode, HttpFileType contentType, const char* message)
{
	if (param->hs && statusCode) param->hs->response.statusCode = statusCode;
	param->contentType = contentType;
	if (!param->pucBuffer || !param->bufSize) {
		param->contentLength = 0;
		return FLAG_DATA_RAW;
	}
	int len = snprintf(param->pucBuffer, param->bufSize, "%s", message ? message : "Archive request failed");
	if (len < 0) len = 0;
	else if ((unsigned int)len >= param->bufSize) len = (int)param->bufSize - 1;
	param->contentLength = (unsigned int)len;
	return FLAG_DATA_RAW;
}

static int appendResponse(char* buffer, size_t bufferSize, size_t* length, const char* format, ...)
{
	if (!buffer || !length || !format || *length >= bufferSize) return -1;
	va_list args;
	va_start(args, format);
	int written = vsnprintf(buffer + *length, bufferSize - *length, format, args);
	va_end(args);
	if (written < 0 || (size_t)written >= bufferSize - *length) return -1;
	*length += (size_t)written;
	return 0;
}

int uhQuery(UrlHandlerParam* param)
{
	if (!param) return FLAG_DATA_RAW;
	param->contentType = HTTPFILETYPE_JSON;
	param->contentLength = 0;
	if (!param->pucBuffer || !param->bufSize)
		return writeArchiveError(param, 500, HTTPFILETYPE_JSON, "{}");

	loadConfig();
	const char* userb64 = mwGetVarValue(param->pxVars, "user", 0);
	if (!userb64 || !*userb64) {
		return writeArchiveError(param, 400, HTTPFILETYPE_JSON, "{}");
	}
	size_t inputLength = strlen(userb64);
	char* user = malloc(inputLength + 1);
	if (!user) return writeArchiveError(param, 500, HTTPFILETYPE_JSON, "{}");
	int decodedLength = base64_decode_chars(userb64, (int)inputLength, user);
	if (decodedLength <= 0 || (size_t)decodedLength > inputLength) {
		free(user);
		return writeArchiveError(param, 400, HTTPFILETYPE_JSON, "{}");
	}
	user[decodedLength] = 0;

	char* devids[4] = {0};
	char* password = 0;
	int devcount = getUserInfo(user, &password, devids, 4);
	free(user);
	if (devcount <= 0) return writeArchiveError(param, 404, HTTPFILETYPE_JSON, "{}");

	size_t length = 0;
	if (appendResponse(param->pucBuffer, param->bufSize, &length, "{\"traccar\":\"%s\",\"devid\":[",
		password ? password : "") < 0) {
		return writeArchiveError(param, 500, HTTPFILETYPE_JSON, "{}");
	}
	for (int i = 0; i < devcount; i++) {
		if (!isValidDeviceID(devids[i])
			|| appendResponse(param->pucBuffer, param->bufSize, &length, "%s\"%s\"",
				i ? "," : "", devids[i]) < 0) {
			return writeArchiveError(param, 500, HTTPFILETYPE_JSON, "{}");
		}
	}
	if (appendResponse(param->pucBuffer, param->bufSize, &length, "]}") < 0)
		return writeArchiveError(param, 500, HTTPFILETYPE_JSON, "{}");
	param->contentLength = (unsigned int)length;
	return FLAG_DATA_RAW;
}

#if 0
int UploadCallback(HttpMultipart *pxMP, OCTET *poData, size_t dwDataChunkSize)
{
  // Do nothing with the data
	int fd = (int)pxMP->pxCallBackData;
	if (!poData) {
		// to cleanup
		if (fd > 0) {
			close(fd);
			pxMP->pxCallBackData = NULL;
		}
		return 0;
	}
	if (fd == 0) {
		char filename[256];
		char* dir = 0;
		time_t t;
		struct tm *btm;
		int n;

		for (n = 0; n < pxMP->pp.iNumParams; n++) {
			if (!strcmp(pxMP->pp.stParams[n].pchParamName, "user")) {
				dir = pxMP->pp.stParams[n].pchParamValue;
			}
		}
		if (!dir) {
			return -1;
		}
		t = time(0);
		btm = localtime(&t);
		n = sprintf(filename, "%02d%02d%02d%02d%02d%02d_", btm->tm_year - 100, btm->tm_mon + 1, btm->tm_mday, btm->tm_hour, btm->tm_min, btm->tm_sec);
		memmove(pxMP->pchFilename + n, pxMP->pchFilename, strlen(pxMP->pchFilename) + 1);
		memcpy(pxMP->pchFilename, filename, n);
		snprintf(filename, sizeof(filename), "data/%s/%s", dir, pxMP->pchFilename);
		fd = open(filename, O_CREAT | O_TRUNC | O_RDWR | O_BINARY, 0);
		pxMP->pxCallBackData = (void*)fd;
		if (fd < 0) {
			pxMP->pchFilename[0] = 0;
			return -1;
		}
	} else if (fd < 0) {
		return 0;
	}
	if (pxMP->bytesReceived > MAX_UPLOAD_SIZE) {
		close(fd);
		return -1;
	}
	write(fd, poData, dwDataChunkSize);
	if (pxMP->oFileuploadStatus & HTTPUPLOAD_LASTCHUNK) {
		close(fd);
		pxMP->pxCallBackData = NULL;
	}
	printf("Received %u bytes for multipart upload file %s\n", dwDataChunkSize, pxMP->pchFilename);
	return 0;
}
#endif

int ConvertToKML(KML_DATA* kd, FILE* fp, const char* kmlfile, uint32_t startpos, uint32_t endpos);
void CleanupKML(KML_DATA* kd);

void WriteGeoJSON(FILE* fpout, KML_DATA* kd, int size, int count)
{
	int pos = fprintf(fpout, "{\"meta\":{\"rev\":%u,\"size\":%u,\"samples\":%u,\"duration\":", META_REVISION, size, count);
	fprintf(fpout, "0         }");

	if (!kd->data) {
		return;
	}

	fprintf(fpout, ",\n");

	DATASET* end;
	for (end = kd->data; end->next; end = end->next);

	fprintf(fpout, "\"stats\":{\"distance\":%u,\"start\":{\"lat\":%f,\"lng\":%f,\"date\":%u,\"time\":%u,\"ts\":%u},\"end\":{\"lat\":%f,\"lng\":%f,\"date\":%u,\"time\":%u,\"ts\":%u}},\n",
		(unsigned int)kd->distance,
		kd->data->lat, kd->data->lng, kd->data->date, kd->data->time, kd->data->timestamp,
		end->lat, end->lng, end->date, end->time, end->timestamp);

	fprintf(fpout, "\"bounds\":[{\"lat\":%f,\"lng\":%f}, {\"lat\":%f,\"lng\":%f}],\n",
		kd->bounds[0].lat, kd->bounds[0].lng, kd->bounds[1].lat, kd->bounds[1].lng);

	fprintf(fpout, "\"pids\":[0");
	for (int n = 1; n < 65536; n++) {
		if (kd->pidMap[n]) fprintf(fpout, ",%u", n);
	}
	fprintf(fpout, "],\n");
	fprintf(fpout, "\"trip\":{\"type\":\"LineString\"");
	DATASET* pd = kd->data;
	fprintf(fpout, ",\"coordinates\":[[%f,%f]", pd->lng, pd->lat);
	while ((pd = pd->next)) fprintf(fpout, ",[%f,%f]", pd->lng, pd->lat);
	fprintf(fpout, "],\n");

	pd = kd->data;
	uint32_t t = kd->data->timestamp;
	fprintf(fpout, "\"timestamps\":[%u", pd->timestamp - t);
	while ((pd = pd->next)) fprintf(fpout, ",%u", pd->timestamp - t);
	fprintf(fpout, "],\n");

	pd = kd->data;
	fprintf(fpout, "\"altitudes\":[%d", (int)pd->alt);
	while ((pd = pd->next)) fprintf(fpout, ",%d", (int)pd->alt);
	fprintf(fpout, "],\n");

	pd = kd->data;
	fprintf(fpout, "\"accels\":[[%d,%d,%d]", pd->acc[0], pd->acc[1], pd->acc[2]);
	while ((pd = pd->next)) fprintf(fpout, ",[%d,%d,%d]", pd->acc[0], pd->acc[1], pd->acc[2]);
	fprintf(fpout, "],\n");

	pd = kd->data;
	fprintf(fpout, "\"battery\":[%.1f", (float)pd->battery / 100);
	while ((pd = pd->next)) fprintf(fpout, ",%.1f", (float)pd->battery / 100);
	fprintf(fpout, "],\n");

	pd = kd->data;
	fprintf(fpout, "\"speeds\":[%.1f", pd->speed);
	while ((pd = pd->next)) fprintf(fpout, ",%.1f", pd->speed);
	fprintf(fpout, "]\n");
	fprintf(fpout, "}\n");

	fprintf(fpout, "}");
	if (end->timestamp > kd->data->timestamp) {
		fseek(fpout, pos, SEEK_SET);
		fprintf(fpout, "%u", end->timestamp - kd->data->timestamp);
	}
}

int CreateDataFiles(KML_DATA* kd, const char* file)
{
	char path[ARCHIVE_PATH_SIZE];
	FILE* fp;
	int count;
	int size;

	if (buildArchivePath(path, sizeof(path), file, ".txt") < 0) return -1;
	fp = fopen(path, "r");
	if (!fp) {
		return -1;
	}
	if (buildArchivePath(path, sizeof(path), file, ".kml") < 0) {
		fclose(fp);
		return -1;
	}
	count = ConvertToKML(kd, fp, path, 0, 0);
	fseek(fp, 0, SEEK_END);
	size = ftell(fp);
	fclose(fp);

	if (buildArchivePath(path, sizeof(path), file, ".json") < 0) return -1;
	fp = fopen(path, "w");
	if (!fp) return -1;
	WriteGeoJSON(fp, kd, size, count);
	fclose(fp);
	return count;
}

int loadMetaInfo(const char* file, uint32_t* duration, uint32_t* size)
{
	FILE* fp = fopen(file, "r");
	int rev = 0;
	if (fp) {
		char buf[256];
		int n = fread(buf, 1, sizeof(buf) - 1, fp);
		fclose(fp);
		if (n > 0) {
			buf[n] = 0;
			char* p;
			if (p = strstr(buf, "\"rev\":")) rev = atoi(p + 6);
			if (duration && (p = strstr(buf, "\"duration\":"))) * duration = atoi(p + 11);
			if (size && (p = strstr(buf, "\"size\":"))) * size = atoi(p + 7);
		}
	}
	return rev;
}

int uhData(UrlHandlerParam* param)
{
	const char* devid = mwGetVarValue(param->pxVars, "devid", 0);
	const char* tripid = mwGetVarValue(param->pxVars, "tripid", 0);
	int64_t offset = mwGetVarValueInt64(param->pxVars, "offset");
	int pidreq = mwGetVarValueInt(param->pxVars, "pid", 0);
	param->contentType = HTTPFILETYPE_TEXT;

	if (!isValidDeviceID(devid)) {
		return writeArchiveError(param, 400, HTTPFILETYPE_TEXT, "Invalid device ID");
	}
	if (!isValidTripID(tripid)) {
		return writeArchiveError(param, 400, HTTPFILETYPE_TEXT, "Invalid arguments");
	}

	char file[ARCHIVE_PATH_SIZE];
	if (buildTripPath(file, sizeof(file), devid, tripid) < 0) {
		return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Archive path too long");
	}
	char path[ARCHIVE_PATH_SIZE];
	if (buildArchivePath(path, sizeof(path), file, ".txt") < 0) {
		return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Archive path too long");
	}

	FILE* fp = fopen(path, "r");
	if (!fp) {
		return writeArchiveError(param, 0, HTTPFILETYPE_TEXT, "Data file not found");
	}

	param->contentType = HTTPFILETYPE_JSON;
	size_t len = 0;
	if (appendResponse(param->pucBuffer, param->bufSize, &len, "[") < 0) {
		fclose(fp);
		return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Response too large");
	}
	uint32_t ts = 0;
	char buf[1024];
	while (fscanf(fp, "%1023s\n", buf) > 0) {
		for (char* p = strtok(buf, ","); p; p = strtok(NULL, ",")) {
			int pid = hex2uint16(p);
			char* separator = strpbrk(p, ":=");
			if (!separator) break;
			char* valuePtr = separator + 1;
			char* checksum = strchr(valuePtr, '*');
			if (checksum) *checksum = 0;
			float value[3] = { 0 };
			int n = 0;
			do {
				value[n++] = (float)atof(valuePtr);
				valuePtr = strchr(valuePtr, ';');
				if (!valuePtr) break;
				valuePtr++;
			} while (n < 3);
			if (pid == 0) {
				ts = (uint32_t)value[0];
				continue;
			}
			if (pid == pidreq) {
				int result;
				if (n == 1) {
					if (pid >= 0x100)
						result = appendResponse(param->pucBuffer, param->bufSize, &len, "[%lld,%d],", (long long)(offset + ts), (int)value[0]);
					else
						result = appendResponse(param->pucBuffer, param->bufSize, &len, "[%lld,%.2f],", (long long)(offset + ts), value[0]);
				}
				else {
					result = appendResponse(param->pucBuffer, param->bufSize, &len, "[%lld,[%d,%d,%d]],", (long long)(offset + ts), (int)value[0], (int)value[1], (int)value[2]);
				}
				if (result < 0) {
					fclose(fp);
					return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Response too large");
				}
			}
		}
	}
	fclose(fp);
	if (len > 0 && param->pucBuffer[len - 1] == ',') len--;
	if (appendResponse(param->pucBuffer, param->bufSize, &len, "]") < 0) {
		return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Response too large");
	}
	param->contentLength = (unsigned int)len;
	return FLAG_DATA_RAW;
}

int processTripData(const char* devid, const char* tripid, int force, char* file, size_t fileSize, uint32_t* psize, uint32_t* pduration)
{
	if (buildTripPath(file, fileSize, devid, tripid) < 0) return -1;

	int processed = 0;

	char path[ARCHIVE_PATH_SIZE];
	if (buildArchivePath(path, sizeof(path), file, ".json") < 0) return -1;
	uint32_t size = 0, duration = 0;
	int rev = loadMetaInfo(path, &duration, &size);
	if (rev == META_REVISION) {
		if (buildArchivePath(path, sizeof(path), file, ".txt") < 0) return -1;
		FILE* fp = fopen(path, "r");
		if (fp) {
			fseek(fp, 0, SEEK_END);
			if (ftell(fp) == size) processed = 1;
			fclose(fp);
		}
		if (psize) *psize = size;
		if (pduration) *pduration = duration;
	}

	if (force || !processed) {
		KML_DATA kd = { 0 };
		int count = CreateDataFiles(&kd, file);
		CleanupKML(&kd);
		if (count <= 0) {
			return -1;
		}
		if (buildArchivePath(path, sizeof(path), file, ".json") < 0) return -1;
		rev = loadMetaInfo(path, &duration, &size);
		if (rev == META_REVISION) {
			if (psize) *psize = size;
			if (pduration) *pduration = duration;
		}
	}

	return 0;
}

static int isSafeRedirectPath(const char* path)
{
	if (!path || path[0] != '/' || path[1] == '/') return 0;
	char segment[2] = {0};
	size_t length = 0;
	for (const unsigned char* input = (const unsigned char*)path; *input; input++) {
		if (*input < 0x20 || *input == 0x7f || *input == '\\') return 0;
		if (*input == '?') {
			return !(length == 2 && segment[0] == '.' && segment[1] == '.');
		}
		if (*input == '/') {
			if (length == 2 && segment[0] == '.' && segment[1] == '.') return 0;
			length = 0;
		} else {
			if (length < 2) segment[length] = (char)*input;
			if (length < 3) length++;
		}
	}
	return !(length == 2 && segment[0] == '.' && segment[1] == '.');
}

int uhTrip(UrlHandlerParam* param)
{
	const char* devid = mwGetVarValue(param->pxVars, "devid", 0);
	const char* tripid = mwGetVarValue(param->pxVars, "tripid", 0);
	const char* redir = mwGetVarValue(param->pxVars, "redir", 0);
	int regen = mwGetVarValueInt(param->pxVars, "regen", 0);
	const char* ext = "json";
	const char* suffix = ".json";
	param->contentType = HTTPFILETYPE_TEXT;

	if (!isValidDeviceID(devid)) {
		return writeArchiveError(param, 400, HTTPFILETYPE_TEXT, "Invalid device ID");
	}
	if (!isValidTripID(tripid)) {
		return writeArchiveError(param, 400, HTTPFILETYPE_TEXT, "Invalid arguments");
	}

	char file[128];
	if (processTripData(devid, tripid, regen, file, sizeof(file), 0, 0) == -1) {
		return writeArchiveError(param, 0, HTTPFILETYPE_JSON, "{\"status\":2,\"error\":\"No data\"}");
	}

	if (!strcmp(param->pucRequest, ".kml")) {
		ext = "kml";
		suffix = ".kml";
		param->contentType = HTTPFILETYPE_XML;
	} else if (!strcmp(param->pucRequest, ".raw")) {
		ext = "txt";
		suffix = ".txt";
		param->contentType = HTTPFILETYPE_TEXT;
	}
	else {
		param->contentType = HTTPFILETYPE_JSON;
	}

	if (!param->pucBuffer || !param->bufSize)
		return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Response buffer unavailable");
	int len;
	if (redir) {
		if (!isSafeRedirectPath(redir))
			return writeArchiveError(param, 400, HTTPFILETYPE_TEXT, "Invalid redirect path");
		len = snprintf(param->pucBuffer, param->bufSize, "%s/%s.%s", redir, file, ext);
		if (len < 0 || (unsigned int)len >= param->bufSize
			|| (unsigned int)len >= sizeof(((HttpFilePath*)0)->cFilePath))
			return writeArchiveError(param, 400, HTTPFILETYPE_TEXT, "Redirect path too long");
		return FLAG_DATA_REDIRECT;
	}
	char path[ARCHIVE_PATH_SIZE];
	if (buildArchivePath(path, sizeof(path), file, suffix) < 0
		|| strlen(path) >= sizeof(((HttpFilePath*)0)->cFilePath)
		|| strlen(path) >= param->bufSize)
		return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Archive path too long");
	memcpy(param->pucBuffer, path, strlen(path) + 1);
	return FLAG_DATA_FILE | FLAG_ABSOLUTE_PATH;
}

void getDateTimeInt(const char* isotime, unsigned int* dateint, unsigned int* timeint)
{
	int year = 0;
	int month = 0;
	int day = 0;
	int hour = 0;
	int minute = 0;
	int second = 0;
	do {
		const char* s = isotime;
		year = atoi(s);
		if (!(s = strchr(s, '-'))) break;
		month = atoi(++s);
		if (!(s = strchr(s, '-'))) break;
		day = atoi(++s);
		if (!(s = strchr(s, 'T'))) break;
		hour = atoi(++s);
		if (!(s = strchr(s, ':'))) break;
		minute = atoi(++s);
		if (!(s = strchr(s, ':'))) break;
		second = atoi(++s);
	} while (0);
	*dateint = year * 10000 + month * 100 + day;
	*timeint = hour * 10000 + minute * 100 + second;
}

void getDateTimeBreakdown(const char* isotime, int* year, int* month, int* day, int* hour, int* minute, int* second)
{
	*year = 0;
	*month = 0;
	*day = 0;
	*hour = 0;
	*minute = 0;
	*second = 0;
	do {
		const char* s = isotime;
		*year = atoi(s);
		if (!(s = strchr(s, '-'))) break;
		*month = atoi(++s);
		if (!(s = strchr(s, '-'))) break;
		*day = atoi(++s);
		if (!(s = strchr(s, 'T'))) break;
		*hour = atoi(++s);
		if (!(s = strchr(s, ':'))) break;
		*minute = atoi(++s);
		if (!(s = strchr(s, ':'))) break;
		*second = atoi(++s);
	} while (0);
}

int uhHistory(UrlHandlerParam* param)
{
	const char* szbegin = mwGetVarValue(param->pxVars, "begin", 0);
	const char* szend = mwGetVarValue(param->pxVars, "end", 0);
	const char* devid = mwGetVarValue(param->pxVars, "devid", 0);

	if (!isValidDeviceID(devid)) {
		return writeArchiveError(param, 400, HTTPFILETYPE_TEXT, "Invalid device ID");
	}
	if (!szbegin || !szend) {
		return writeArchiveError(param, 400, HTTPFILETYPE_TEXT, "Invalid arguments");
	}

	unsigned int beginDate = 0, beginTime = 0, endDate = 0, endTime = 0;
	getDateTimeInt(szbegin, &beginDate, &beginTime);
	getDateTimeInt(szend, &endDate, &endTime);
	if (beginDate == 0 || endDate == 0 || beginDate > endDate) {
		return writeArchiveError(param, 400, HTTPFILETYPE_TEXT, "Invalid arguments");
	}

	char path[ARCHIVE_PATH_SIZE];
	if (buildArchivePath(path, sizeof(path), devid, "") < 0) {
		return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Archive path too long");
	}
	if (!IsDir(path)) {
		return writeArchiveError(param, 404, HTTPFILETYPE_JSON, "[]");
	}

	int year, month, day, hour, minute, second;
	getDateTimeBreakdown(szbegin, &year, &month, &day, &hour, &minute, &second);

	if (!param->pucBuffer || !param->bufSize)
		return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Response buffer unavailable");
	char *pb = param->pucBuffer;
	size_t bs = param->bufSize;
	size_t n = 0;
	if (appendResponse(pb, bs, &n, "[\n") < 0)
		return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Response too large");
	int eod = 0;
	int count = 0;
	for (unsigned int date = beginDate; date <= endDate && count <= 365; count++) {
		int pathLen = snprintf(path, sizeof(path), "%s/%s/%04u/%02u/%02u",
			dataDir, devid, year, month, day);
		if (pathLen < 0 || (size_t)pathLen >= sizeof(path))
			return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Archive path too long");
		char file[260];
		if (ReadDir(path, file) == 0) {
			do {
				char *q = strchr(file, '.');
				if (!q || strcmp(q + 1, "txt")) continue;
				*q = 0;
				if (!isValidTripID(file)) continue;
				unsigned int time = atoi(file + 9);
				date = atoi(file);
				if (date < beginDate || date > endDate)
					continue;
				if ((date == beginDate && time < beginTime) || (date == endDate && endTime && time > endTime))
					continue;

				// retrieve meta data
				uint32_t duration = 0;
				uint32_t size = 0;
				char filepath[128];
				if (processTripData(devid, file, 0, filepath, sizeof(filepath), &size, &duration) == -1) {
					continue;
				}

				hour = time / 10000;
				minute = (time / 100) % 100;
				second = time % 100;
				struct tm t = { second, minute, hour, day, month - 1, year - 1900 };
				time_t tm = mktime(&t);
				if (appendResponse(pb, bs, &n, "{\"id\":\"%s\",\"key\":%u,\"utc\":\"%04u-%02u-%02uT%02u:%02u:%02uZ\",\"size\":%u,\"duration\":%u},",
					file, (unsigned int)tm,
					year, month, day, hour, minute, second,
					size, duration) < 0) {
					ReadDir(NULL, NULL);
					return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Response too large");
				}
			} while (ReadDir(0, file) == 0);
		}

		if (!eod) {
			if (month == 4 || month == 6 || month == 9 || month == 11)
				eod = 30;
			else if (month == 2)
				eod = (year % 4 == 0) ? 29 : 28;
			else
				eod = 31;
		}

		if (++day > eod) {
			day = 1;
			if (++month > 12) {
				month = 1;
				year++;
			}
			eod = 0;
		}
		date = year * 10000 + month * 100 + day;
	}
	if (n > 0 && pb[n - 1] == ',') n--;
	if (appendResponse(pb, bs, &n, "]") < 0)
		return writeArchiveError(param, 500, HTTPFILETYPE_TEXT, "Response too large");
	param->contentLength = (unsigned int)n;
	param->contentType = HTTPFILETYPE_JSON;
	return FLAG_DATA_RAW;
}
