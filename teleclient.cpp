/******************************************************************************
* Freematics Hub client and Traccar client implementations
* Works with Freematics ONE+
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
#include "telestore.h"
#include "teleclient.h"
#include "config.h"
#if HTTP_COMPRESS_UPLOADS && SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
#include "esp32/rom/miniz.h"
#endif

extern int16_t rssi;
extern char devid[];
extern char vin[];
extern GPS_DATA* gd;
extern char isoTime[];

CBuffer::CBuffer(uint8_t* mem)
{
  m_data = mem;
  state = BUFFER_STATE_EMPTY;
  purge();
}

bool CBuffer::add(uint16_t pid, uint8_t type, void* values, int bytes, uint8_t count)
{
    if (!m_data || !values || bytes < 0
        || (uint32_t)offset + sizeof(ELEMENT_HEAD) + (uint32_t)bytes > BUFFER_LENGTH) {
        Serial.println("FULL");
        return false;
    }
    ELEMENT_HEAD hdr = {pid, type, count};
    *(ELEMENT_HEAD*)(m_data + offset) = hdr;
    offset += sizeof(ELEMENT_HEAD);
    memcpy(m_data + offset, values, bytes);
    offset += bytes;
    total++;
    return true;
}

void CBuffer::purge()
{
  timestamp = 0;
  offset = 0;
  total = 0;
  recorded = false;
}

void CBuffer::serialize(CStorage& store)
{
  uint16_t of = 0;
  for (int n = 0; n < total && of < offset; n++) {
    ELEMENT_HEAD* hdr = (ELEMENT_HEAD*)(m_data + of);
    of += sizeof(ELEMENT_HEAD);
    switch (hdr->type) {
    case ELEMENT_UINT8:
      store.log(hdr->pid, (uint8_t*)(m_data + of), hdr->count);
      of += (uint16_t)hdr->count * sizeof(uint8_t);
      break;
    case ELEMENT_UINT16:
      store.log(hdr->pid, (uint16_t*)(m_data + of), hdr->count);
      of += (uint16_t)hdr->count * sizeof(uint16_t);
      break;
    case ELEMENT_UINT32:
      store.log(hdr->pid, (uint32_t*)(m_data + of), hdr->count);
      of += (uint16_t)hdr->count * sizeof(uint32_t);
      break;
    case ELEMENT_INT32:
      store.log(hdr->pid, (int32_t*)(m_data + of), hdr->count);
      of += (uint16_t)hdr->count * sizeof(int32_t);
      break;
    case ELEMENT_FLOAT:
      store.log(hdr->pid, (float*)(m_data + of), hdr->count);
      of += (uint16_t)hdr->count * sizeof(float);
      break;
    case ELEMENT_FLOAT_D1:
      store.log(hdr->pid, (float*)(m_data + of), hdr->count, "%.1f");
      of += (uint16_t)hdr->count * sizeof(float);
      break;
    case ELEMENT_FLOAT_D2:
      store.log(hdr->pid, (float*)(m_data + of), hdr->count, "%.2f");
      of += (uint16_t)hdr->count * sizeof(float);
      break;
    default:
      return;
    }
  }
}

void CBufferManager::init()
{
  total = BUFFER_SLOTS;
#if BOARD_HAS_PSRAM
    slots = (CBuffer**)heap_caps_malloc(BUFFER_SLOTS * sizeof(void*), MALLOC_CAP_SPIRAM);
#else
    slots = (CBuffer**)malloc(BUFFER_SLOTS * sizeof(void*));
#endif
  for (int n = 0; n < BUFFER_SLOTS; n++) {
    void* mem;
#if BOARD_HAS_PSRAM
    mem = heap_caps_malloc(BUFFER_LENGTH, MALLOC_CAP_SPIRAM);
#else
    mem = malloc(BUFFER_LENGTH);
#endif
    if (!mem) {
      Serial.println("OUT OF RAM");
      total = n;
      break;
    }
    slots[n] = new CBuffer((uint8_t*)mem);
  }
  assert(total > 0);
}

void CBufferManager::purge()
{
  portENTER_CRITICAL(&m_mux);
  for (int n = 0; n < total; n++) {
    slots[n]->purge();
    slots[n]->state = BUFFER_STATE_EMPTY;
  }
  last = 0;
  portEXIT_CRITICAL(&m_mux);
}

CBuffer* CBufferManager::getFree()
{
  CBuffer* freeSlot = 0;
  portENTER_CRITICAL(&m_mux);
  if (last) {
    CBuffer* slot = last;
    last = 0;
    if (slot->state == BUFFER_STATE_EMPTY) {
      slot->recorded = false;
      slot->state = BUFFER_STATE_FILLING;
      freeSlot = slot;
    }
  }
  // A full queue must never overwrite a captured reading.
  for (int n = 0; !freeSlot && n < total; n++) {
    if (slots[n]->state == BUFFER_STATE_EMPTY) {
      slots[n]->recorded = false;
      slots[n]->state = BUFFER_STATE_FILLING;
      freeSlot = slots[n];
    }
  }
  portEXIT_CRITICAL(&m_mux);
  return freeSlot;
}

CBuffer* CBufferManager::getOldest(bool recorded)
{
  uint32_t ts = 0;
  int m = -1;
  portENTER_CRITICAL(&m_mux);
  for (int n = 0; n < total; n++) {
    // RAM queue entries span less than half the 32-bit millisecond period.
    if (slots[n]->state == BUFFER_STATE_FILLED && slots[n]->recorded == recorded &&
        (m < 0 || (int32_t)(slots[n]->timestamp - ts) < 0)) {
        m = n;
        ts = slots[n]->timestamp;
    }
  }
  if (m >= 0) {
    slots[m]->state = BUFFER_STATE_LOCKED;
  }
  CBuffer* result = m >= 0 ? slots[m] : 0;
  portEXIT_CRITICAL(&m_mux);
  return result;
}

CBuffer* CBufferManager::getNewest()
{
  uint32_t ts = 0;
  int m = -1;
  portENTER_CRITICAL(&m_mux);
  for (int n = 0; n < total; n++) {
    if (slots[n]->state == BUFFER_STATE_FILLED &&
        (m < 0 || (int32_t)(slots[n]->timestamp - ts) > 0)) {
      m = n;
      ts = slots[n]->timestamp;
    }
  }
  if (m >= 0) {
    slots[m]->state = BUFFER_STATE_LOCKED;
  }
  CBuffer* result = m >= 0 ? slots[m] : 0;
  portEXIT_CRITICAL(&m_mux);
  return result;
}

void CBufferManager::free(CBuffer* slot)
{
  slot->purge();
  portENTER_CRITICAL(&m_mux);
  slot->state = BUFFER_STATE_EMPTY;
  last = slot;  
  portEXIT_CRITICAL(&m_mux);
}

void CBufferManager::publish(CBuffer* slot)
{
  portENTER_CRITICAL(&m_mux);
  slot->state = BUFFER_STATE_FILLED;
  portEXIT_CRITICAL(&m_mux);
}

void CBufferManager::restore(CBuffer* slot)
{
  portENTER_CRITICAL(&m_mux);
  slot->state = BUFFER_STATE_FILLED;
  portEXIT_CRITICAL(&m_mux);
}

void CBufferManager::recordMissedReading(uint32_t count)
{
  portENTER_CRITICAL(&m_mux);
  missed += count;
  portEXIT_CRITICAL(&m_mux);
}

uint32_t CBufferManager::missedReadings() const
{
  portENTER_CRITICAL(&m_mux);
  uint32_t result = missed;
  portEXIT_CRITICAL(&m_mux);
  return result;
}

void CBufferManager::printStats()
{
  int bytes = 0;
  int count = 0;
  int samples = 0;
  portENTER_CRITICAL(&m_mux);
  for (int n = 0; n < total; n++) {
    if (slots[n]->state != BUFFER_STATE_FILLED) continue;
    bytes += slots[n]->offset;
    samples += slots[n]->total;
    count++;
  }
  portEXIT_CRITICAL(&m_mux);
  if (slots) {
    Serial.print("[QUEUE] Waiting readings: ");
    Serial.print(count);
    Serial.print(" | values: ");
    Serial.print(samples);
    Serial.print(" | memory: ");
    Serial.print(bytes);
    Serial.print(" bytes | capacity: ");
    Serial.print(total);
    Serial.println(" readings");
  }
}
uint16_t CBufferManager::pendingReadings() const
{
  if (!slots) return 0;
  uint16_t count = 0;
  portENTER_CRITICAL(&m_mux);
  for (uint32_t n = 0; n < total; n++) {
    if (slots[n]->state == BUFFER_STATE_FILLED) count++;
  }
  portEXIT_CRITICAL(&m_mux);
  return count;
}

uint16_t CBufferManager::unpersistedReadings() const
{
  if (!slots) return 0;
  uint16_t count = 0;
  portENTER_CRITICAL(&m_mux);
  for (uint32_t n = 0; n < total; n++) {
    // An HTTP request can own a LOCKED buffer while the ordinary pending
    // count is zero. It is still the only copy until the server accepts it.
    if (slots[n]->state != BUFFER_STATE_EMPTY) count++;
  }
  portEXIT_CRITICAL(&m_mux);
  return count;
}

uint32_t CBufferManager::pendingBytes() const
{
  if (!slots) return 0;
  uint32_t bytes = 0;
  portENTER_CRITICAL(&m_mux);
  for (uint32_t n = 0; n < total; n++) {
    if (slots[n]->state == BUFFER_STATE_FILLED) bytes += slots[n]->offset;
  }
  portEXIT_CRITICAL(&m_mux);
  return bytes;
}

bool TeleClientUDP::verifyChecksum(char* data)
{
  uint8_t sum = 0;
  char *s = strrchr(data, '*');
  if (!s) return false;
  for (char *p = data; p < s; p++) sum += *p;
  if (hex2uint8(s + 1) == sum) {
    *s = 0;
    return true;
  }
  return false;
}

bool TeleClientUDP::notify(byte event, const char* payload)
{
  char buf[48];
  char cache[128];
  CStorageRAM netbuf;
  netbuf.init(cache, 128);
  netbuf.header(devid);
  netbuf.dispatch(buf, sprintf(buf, "EV=%X", (unsigned int)event));
  netbuf.dispatch(buf, sprintf(buf, "TS=%lu", millis()));
  netbuf.dispatch(buf, sprintf(buf, "ID=%s", devid));
  if (rssi) {
    netbuf.dispatch(buf, sprintf(buf, "SSI=%d", (int)rssi));
  }
  if (vin[0]) {
    netbuf.dispatch(buf, sprintf(buf, "VIN=%s", vin));
  }
  if (payload) {
    netbuf.dispatch(payload, strlen(payload));
  }
  netbuf.tailer();
  //Serial.println(netbuf.buffer());
  for (byte attempts = 0; attempts < 3; attempts++) {
    // send notification datagram
#if ENABLE_WIFI
    if (wifi.connected())
    {
      if (!wifi.send(netbuf.buffer(), netbuf.length())) break;
    }
    else
#endif
    {
      if (!cell.send(netbuf.buffer(), netbuf.length())) break;
    }
    if (event == EVENT_ACK) return true; // no reply for ACK
    char *data = 0;
    int bytesRecv = 0;
    // receive reply
#if ENABLE_WIFI
    if (wifi.connected())
    {
      data = cell.getBuffer();
      bytesRecv = wifi.receive(data, RECV_BUF_SIZE - 1);
      if (bytesRecv > 0) {
        data[bytesRecv] = 0;
      }
    }
    else
#endif
    {
      data = cell.receive(&bytesRecv); 
    }
    if (!data || bytesRecv == 0) {
      Serial.println("[UDP] Timeout");
      continue;
    }
    rxBytes += bytesRecv;
    // verify checksum
    if (!verifyChecksum(data)) {
      Serial.print("[UDP] Checksum mismatch:");
      Serial.println(data);
      continue;
    }
    char pattern[16];
    sprintf(pattern, "EV=%u", event);
    if (!strstr(data, pattern)) {
      Serial.print("[UDP] Invalid reply: ");
      Serial.println(data);
      continue;
    }
    if (event == EVENT_LOGIN) {
      // extract info from server response
      char *p = strstr(data, "TM=");
      if (p) {
        // set local time from server
        unsigned long tm = atol(p + 3);
        struct timeval tv = { .tv_sec = (time_t)tm, .tv_usec = 0 };
        settimeofday(&tv, NULL);
      }
      p = strstr(data, "SN=");
      if (p) {
        char *q = strchr(p, ',');
        if (q) *q = 0;
      }
      feedid = hex2uint16(data);
      login = true;
    } else if (event == EVENT_LOGOUT) {
      login = false;
    }
    // success
    return true;
  }
  return false;
}

bool TeleClientUDP::connect(bool quick)
{
  byte event = login ? EVENT_RECONNECT : EVENT_LOGIN;
  bool success = false;
#if ENABLE_WIFI
  if (wifi.connected())
  {
    if (quick) return wifi.open(SERVER_HOST, SERVER_PORT);
  }
  else
#endif
  {
    cell.close();
    if (quick) {
      return cell.open(0, 0);
    }
  }

  packets = 0;

  // connect to telematics server
  for (byte attempts = 0; attempts < 3; attempts++) {
    Serial.print(event == EVENT_LOGIN ? "LOGIN(" : "RECONNECT(");
    Serial.print(SERVER_HOST);
    Serial.print(':');
    Serial.print(SERVER_PORT);
    Serial.println(")...");
#if ENABLE_WIFI
    if (wifi.connected())
    {
      if (!wifi.open(SERVER_HOST, SERVER_PORT)) {
        Serial.println("[WIFI] Unable to connect");
        delay(1000);
        continue;
      }
    }
    else
#endif
    {
      if (!cell.open(SERVER_HOST, SERVER_PORT)) {
        if (!cell.check()) break;
        Serial.println("[NET] Unable to connect");
        delay(3000);
        continue;
      }
    }
    // log in or reconnect to Freematics Hub
    if (!notify(event)) {
#if ENABLE_WIFI
      if (wifi.connected())
      {
        wifi.close();
      }
      else
#endif
      {
        if (!cell.check()) break;
        cell.close();
      }
      Serial.println("[NET] Server timeout");
      continue;
    }
    success = true;
    break;
  }
  if (event == EVENT_LOGIN) startTime = millis();
  if (success) {
    lastSyncTime = millis();
  }
  return success;
}

bool TeleClientUDP::ping()
{
  bool success = false;
  for (byte n = 0; n < 3 && !success; n++) {
#if ENABLE_WIFI
    if (wifi.connected())
    {
      success = wifi.open(SERVER_HOST, SERVER_PORT);
    }
    else
#endif
    {
      success = cell.open(SERVER_HOST, SERVER_PORT);
    }
    if (success) {
      if ((success = notify(EVENT_PING))) break;
#if ENABLE_WIFI
      if (wifi.connected())
      {
        wifi.close();
      }
      else
#endif
      {
        cell.close();
      }
      delay(1000);
    }
  }
  if (success) lastSyncTime = millis();
  return success;
}

bool TeleClientUDP::transmit(const char* packetBuffer, unsigned int packetSize)
{
#if ENABLE_WIFI
  // transmit data via wifi
  if (wifi.connected()) {
    if (wifi.send(packetBuffer, packetSize)) {
      txBytes += packetSize;
      txCount++;
      Serial.print("[WIFI] ");
      Serial.print(packetSize);
      Serial.println(" bytes sent");
      return true;  
    }
    return false;
  }
#endif

  // transmit data via cellular
  if (++packets >= 64) {
    cell.close();
    cell.open(0, 0);
    packets = 0;
  }
  Serial.print("[CELL] ");
  Serial.print(packetSize);
  Serial.println(" bytes being sent");
  if (cell.send(packetBuffer, packetSize)) {
    txBytes += packetSize;
    txCount++;
    return true;
  }
  return false;
}

void TeleClientUDP::inbound()
{
  // check incoming datagram
  do {
    int len = 0;
    char *data = 0;
#if ENABLE_WIFI
    if (wifi.connected())
    {
      data = cell.getBuffer();
      len = wifi.receive(data, RECV_BUF_SIZE - 1, 10);
    }
    else
#endif
    {
      data = cell.receive(&len, 50);
    }
    if (!data || len == 0) break;
    data[len] = 0;
    Serial.print("[UDP] ");
    Serial.println(data);
    rxBytes += len;
    if (!verifyChecksum(data)) {
      Serial.print("[UDP] Checksum mismatch:");
      Serial.println(data);
      break;
    }
    char *p = strstr(data, "EV=");
    if (!p) break;
    int eventID = atoi(p + 3);
    switch (eventID) {
    case EVENT_SYNC:
        feedid = hex2uint16(data);
        Serial.print("[UDP] FEED ID:");
        Serial.println(feedid);
        break;
    }
    lastSyncTime = millis();
  } while(0);
}

void TeleClientUDP::shutdown()
{
  if (login) {
    notify(EVENT_LOGOUT);
    login = false;
    Serial.println("[NET] Logout");
  }
#if ENABLE_WIFI
  if (wifi.connected()) {
    wifi.end();
    Serial.println("[WIFI] Deactivated");
    return;
  }
#endif
  cell.end();
  Serial.println("[CELL] Deactivated");
}

bool TeleClientHTTP::notify(byte event, const char* payload)
{
  char path[256];
  snprintf(path, sizeof(path), "%s/notify/%s?EV=%u&SSI=%d&TS=%lu&VIN=%s", SERVER_PATH, devid,
    (unsigned int)event, (int)rssi, (unsigned long)millis(), vin);
  if (event == EVENT_LOGOUT) login = false;
#if ENABLE_WIFI
  if (m_useWifi)
  {
    if (!wifi.send(METHOD_POST, path)) {
      Serial.println("[HTTP] Notification send failed via Wi-Fi");
      return false;
    }
    char* response = wifi.receive(cell.getBuffer(), RECV_BUF_SIZE - 1);
    if (!response) {
      Serial.println("[HTTP] Notification response timed out via Wi-Fi");
      return false;
    }
    if (wifi.code() == 200) return true;
    Serial.print("[HTTP] Notification rejected via Wi-Fi (status ");
    Serial.print(wifi.code());
    Serial.println(')');
    return false;
  }
#endif
  {
    if (!cell.send(METHOD_POST, SERVER_HOST, SERVER_PORT, path, 0, 0)) {
      Serial.println("[HTTP] Notification send failed via cellular");
      return false;
    }
    char* response = cell.receive();
    if (!response) {
      Serial.println("[HTTP] Notification response timed out via cellular");
      return false;
    }
    if (cell.code() == 200) return true;
    Serial.print("[HTTP] Notification rejected via cellular (status ");
    Serial.print(cell.code());
    Serial.println(')');
    return false;
  }
}

#if HTTP_COMPRESS_UPLOADS && SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
// zlib-compress one batch with the ESP32 ROM deflate. Full-rate samples repeat
// most PIDs and values, so a 40-sample batch shrinks about 6x. Returns 0 when
// compression fails; the caller then sends the batch as it is.
size_t deflateBatch(const char* input, size_t length, uint8_t* output, size_t capacity)
{
  // About 100 KB of compressor state, allocated once in PSRAM.
  static tdefl_compressor* compressor =
    (tdefl_compressor*)heap_caps_malloc(sizeof(tdefl_compressor), MALLOC_CAP_SPIRAM);
  if (!compressor || !input || !output) return 0;
  if (tdefl_init(compressor, nullptr, nullptr, TDEFL_WRITE_ZLIB_HEADER | HTTP_COMPRESS_PROBES) != TDEFL_STATUS_OKAY) {
    return 0;
  }
  size_t consumed = length;
  size_t produced = capacity;
  const tdefl_status status = tdefl_compress(compressor, input, &consumed, output, &produced, TDEFL_FINISH);
  return status == TDEFL_STATUS_DONE && consumed == length ? produced : 0;
}

// The ROM compressor runs without the PSRAM cache workaround this revision 1
// chip needs, and one compressed batch failed to inflate on the collector.
// Inflate the result into an internal-RAM window with the ROM decompressor
// and compare it with the original. Any doubt sends the batch uncompressed.
bool deflateMatches(const uint8_t* packed, size_t packedSize, const char* original, size_t length)
{
  tinfl_decompressor* inflator = (tinfl_decompressor*)heap_caps_malloc(sizeof(tinfl_decompressor),
                                                                      MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  uint8_t* window = (uint8_t*)heap_caps_malloc(TINFL_LZ_DICT_SIZE, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  bool matches = inflator && window;
  if (matches) {
    tinfl_init(inflator);
    size_t read = 0, written = 0, compared = 0;
    for (;;) {
      size_t inBytes = packedSize - read;
      size_t outBytes = TINFL_LZ_DICT_SIZE - written;
      const tinfl_status status = tinfl_decompress(inflator, packed + read, &inBytes, window, window + written,
                                                   &outBytes, TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_COMPUTE_ADLER32);
      read += inBytes;
      if (compared + outBytes > length || memcmp(window + written, original + compared, outBytes)) {
        matches = false;
        break;
      }
      compared += outBytes;
      written = (written + outBytes) & (TINFL_LZ_DICT_SIZE - 1);
      if (status == TINFL_STATUS_DONE) { matches = compared == length && read == packedSize; break; }
      if (status != TINFL_STATUS_HAS_MORE_OUTPUT) { matches = false; break; }
    }
  }
  heap_caps_free(window);
  heap_caps_free(inflator);
  return matches;
}
#endif

bool TeleClientHTTP::transmit(const char* packetBuffer, unsigned int packetSize)
{
#if HTTP_COMPRESS_UPLOADS && SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
  static uint8_t* packed = (uint8_t*)heap_caps_malloc(SERIALIZE_BUFFER_SIZE, MALLOC_CAP_SPIRAM);
  size_t packedSize = packed ? deflateBatch(packetBuffer, packetSize, packed, SERIALIZE_BUFFER_SIZE) : 0;
  if (packedSize && packedSize < packetSize && !deflateMatches(packed, packedSize, packetBuffer, packetSize)) {
    compressionFaults++;
    Serial.print("[HTTP] Compressed batch failed its self-check; sending uncompressed. Faults: ");
    Serial.println(compressionFaults);
    packedSize = 0;
  }
  if (packedSize && packedSize < packetSize) {
    if (transmitBody(packetBuffer, packetSize, (const char*)packed, packedSize)) return true;
    // A collector without inflate support, or a compression fault, must not
    // make the device set readings aside. Resend the batch as it is.
    if (lastStatus != 400) return false;
    Serial.println("[HTTP] Compressed batch refused; resending uncompressed");
  }
#endif
  return transmitBody(packetBuffer, packetSize, nullptr, 0);
}

bool TeleClientHTTP::transmitBody(const char* packetBuffer, unsigned int packetSize,
                                  const char* packed, unsigned int packedSize)
{
  lastStatus = 0;
#if ENABLE_WIFI
  bool disconnected = m_useWifi ? wifi.state() != HTTP_CONNECTED : cell.state() != HTTP_CONNECTED;
  if (disconnected) {
#else
  if (cell.state() != HTTP_CONNECTED) {
#endif
    // reconnect if disconnected
    if (!connect(true)) {
      return false;
    }
  }

  char path[256];
  bool success = false;
  int len;
#if SERVER_PROTOCOL == PROTOCOL_HTTPS_GET
  if (gd && gd->ts) {
    len = snprintf(path, sizeof(path), "%s/push?id=%s&timestamp=%s&lat=%f&lon=%f&altitude=%d&speed=%f&heading=%d",
      SERVER_PATH, devid, isoTime,
      gd->lat, gd->lng, (int)gd->alt, gd->speed, (int)gd->heading);
  } else {
    len = snprintf(path, sizeof(path), "%s/push?id=%s", SERVER_PATH, devid);
  }
  if (len < 0 || (size_t)len >= sizeof(path)) {
    Serial.println("[HTTP] GET path too long");
    return false;
  }
#if ENABLE_WIFI
  if (m_useWifi) {
    Serial.println("[HTTP] GET via Wi-Fi");
    success = wifi.send(METHOD_GET, path);
  }
  else
#endif
  {
    Serial.println("[HTTP] GET via cellular");
    success = cell.send(METHOD_GET, SERVER_HOST, SERVER_PORT, path);
  }
#else
  len = snprintf(path, sizeof(path), packed ? "%s/post/%s?z=1" : "%s/post/%s", SERVER_PATH, devid);
  // The field count check below always uses the uncompressed batch.
  const char* body = packed ? packed : packetBuffer;
  const unsigned int bodySize = packed ? packedSize : packetSize;
#if ENABLE_WIFI
  if (m_useWifi) {
    Serial.print("[HTTP] POST via Wi-Fi: ");
    Serial.println(path);
    success = wifi.send(METHOD_POST, path, body, bodySize);
  }
  else
#endif
  {
    Serial.print("[HTTP] POST via cellular: ");
    Serial.println(path);
    success = cell.send(METHOD_POST, SERVER_HOST, SERVER_PORT, path, body, bodySize);
  }
  len += bodySize;
#endif
  if (!success) {
    Serial.println("[HTTP] Connection closed");
    return false;
  } else {
    txBytes += len;
    txCount++;
  }

  // check response
  int recvBytes = 0;
  char* content = 0;
#if ENABLE_WIFI
  if (m_useWifi)
  {
    content = wifi.receive(cell.getBuffer(), RECV_BUF_SIZE - 1, &recvBytes);
  }
  else
#endif
  {
    content = cell.receive(&recvBytes, HTTP_CONN_TIMEOUT);
  }
  if (!content) {
    // close connection on receiving timeout
    Serial.println("[HTTP] No response");
    return false;
  }
#if ENABLE_WIFI
  int responseCode = m_useWifi ? wifi.code() : cell.code();
#else
  int responseCode = cell.code();
#endif
  lastStatus = responseCode;
  bool accepted = responseCode == 200;
#if SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
  if (accepted) {
    // A proxy or captive portal can answer 200 without ingesting telemetry.
    // The collector returns the exact number of non-timestamp fields stored.
    unsigned int expected = 0;
    for (unsigned int at = 0; at < packetSize;) {
      unsigned int end = at;
      while (end < packetSize && packetBuffer[end] != ',' && packetBuffer[end] != '*') end++;
      if (end > at && !(end - at >= 2 && packetBuffer[at] == '0' && packetBuffer[at + 1] == ':')) expected++;
      // The final '*' introduces a checksum, not another telemetry field.
      if (end == packetSize || packetBuffer[end] == '*') break;
      at = end + 1;
    }
    char* countEnd = 0;
    unsigned long reported = !strncmp(content, "OK ", 3) ? strtoul(content + 3, &countEnd, 10) : 0;
    accepted = expected > 0 && countEnd && countEnd != content + 3 && reported == expected;
    if (!accepted) {
      Serial.print("[HTTP] Collector acknowledgement mismatch; expected fields: ");
      Serial.print(expected);
      Serial.print(" | response: ");
      Serial.println(content);
    }
  }
#endif
  if (accepted) {
    if (!strncmp(content, "OK ", 3)) {
      Serial.print("[HTTP] Server accepted ");
      Serial.print(content + 3);
      Serial.println(" values");
    } else {
      Serial.print("[HTTP] Server accepted request: ");
      Serial.println(content);
    }
    // successful
    lastDataSyncTime = lastSyncTime = millis();
    rxBytes += recvBytes;
  } else {
    Serial.print("[HTTP] Server rejected request (status ");
    Serial.print(responseCode);
    Serial.print("): ");
    Serial.println(content);
  }
  return accepted;
}

bool TeleClientHTTP::connect(bool quick)
{
  if (!SERVER_TOKEN[0]) {
    // The collector is deliberately protected at both Caddy boundaries.
    // Refuse to cycle the modem when this production credential is absent.
    Serial.println("[AUTH] Telemetry token missing");
    return false;
  }
#if SERVER_PROTOCOL == PROTOCOL_HTTPS_GET || SERVER_PROTOCOL == PROTOCOL_HTTPS_POST
  if (SERVER_PORT != 443) {
    Serial.println("[AUTH] HTTPS telemetry requires port 443");
    return false;
  }
#endif
#if ENABLE_WIFI
  wifi.setBearerToken(SERVER_TOKEN);
#endif
  cell.setBearerToken(SERVER_TOKEN);
  Serial.println("[AUTH] Bearer token enabled");

  if (!quick) {
#if ENABLE_WIFI
    if (!wifi.connected()) cell.init();
#else
    cell.init();
#endif
  } else {
#if ENABLE_WIFI
    if (!wifi.connected()) cell.close();
#else
    cell.close();
#endif
  }

  // connect to HTTP server
  bool success = false;

#if ENABLE_WIFI
  if (wifi.connected()) {
    success = wifi.open(SERVER_HOST, SERVER_PORT);
    m_useWifi = success;
    if (!success) Serial.println("[NET] Wi-Fi HTTPS failed; trying cellular");
  }
#endif
  if (!success) {
    m_useWifi = false;
    for (byte attempts = 0; !success && attempts < 3; attempts++) {
      success = cell.open(SERVER_HOST, SERVER_PORT);
      if (!success) {
        if (!cell.check()) break;
        cell.close();
        cell.init();
      }
    }
  }
  if (!success) {
    Serial.println("[NET] Unable to open HTTPS on either transport");
    return false;
  }
  if (quick) return true;
  if (!login) {
    Serial.print("LOGIN(");
    Serial.print(SERVER_HOST);
    Serial.print(':');
    Serial.print(SERVER_PORT);
    Serial.println(")...");
    // log in or reconnect to Freematics Hub
    if (notify(EVENT_LOGIN)) {
      lastSyncTime = millis();
      login = true;
    } else {
      Serial.println("[HTTP] Login rejected");
      return false;
    }
  }
  return true;
}

bool TeleClientHTTP::ping()
{
  // Standby pings must not create a new telemetry session or archive file.
  // Open the authenticated socket, send the lightweight EVENT_PING marker,
  // then let the standby task close the modem again.
  if (!connect(true)) return false;
  bool success = notify(EVENT_PING);
  if (success) lastSyncTime = millis();
  return success;
}

void TeleClientHTTP::shutdown()
{
  if (login) {
    notify(EVENT_LOGOUT);
    login = false;
    Serial.println("[NET] Logout");
  }
#if ENABLE_WIFI
  if (wifi.connected()) {
    wifi.end();
    Serial.println("[WIFI] Deactivated");
  }
#endif
  cell.close();
  cell.end();
  Serial.println("[CELL] Deactivated");
  m_useWifi = false;
}

uint16_t CBufferManager::recordedReadings() const
{
  uint16_t count = 0;
  portENTER_CRITICAL(&m_mux);
  for (uint32_t n = 0; n < total; n++) {
    if (slots[n]->state == BUFFER_STATE_FILLED && slots[n]->recorded) count++;
  }
  portEXIT_CRITICAL(&m_mux);
  return count;
}
