#pragma once

#include <ctype.h>
#include <stdint.h>
#include <string.h>
#include <strings.h>

namespace freematics {
namespace cell {

static const size_t kHttpLocationCapacity = 2048;

struct HTTPResponseInfo {
  uint16_t status;
  uint32_t contentLength;
  char location[kHttpLocationCapacity];
};

typedef int (*HTTPByteReader)(void* context, unsigned char* bytes,
                              size_t capacity);
typedef bool (*HTTPBodyWriter)(void* context, const unsigned char* bytes,
                               size_t length);

namespace detail {

static const size_t kHeaderLineCapacity = 2048;
static const size_t kHeaderBytesLimit = 8192;
static const unsigned kHeaderLineLimit = 64;
static const size_t kBodyChunkCapacity = 256;

inline bool readByte(HTTPByteReader reader, void* context, unsigned char* byte)
{
  const int received = reader(context, byte, 1);
  return received == 1;
}

inline bool readLine(HTTPByteReader reader, void* context, char* output,
                     size_t capacity, size_t* lineLength)
{
  size_t length = 0;
  while (length + 1 < capacity) {
    unsigned char byte;
    if (!readByte(reader, context, &byte) || byte == 0) return false;
    if (byte == '\n') {
      if (!length || output[length - 1] != '\r') return false;
      --length;
      output[length] = 0;
      *lineLength = length;
      return true;
    }
    output[length++] = static_cast<char>(byte);
  }
  return false;
}

inline bool validStatusLine(const char* line, size_t length, unsigned* status)
{
  if (length < 12 || strncmp(line, "HTTP/1.", 7) ||
      (line[7] != '0' && line[7] != '1') || line[8] != ' ' ||
      !isdigit(static_cast<unsigned char>(line[9])) ||
      !isdigit(static_cast<unsigned char>(line[10])) ||
      !isdigit(static_cast<unsigned char>(line[11])) ||
      (line[12] && line[12] != ' ')) return false;
  for (size_t i = 12; i < length; ++i) {
    const unsigned char byte = static_cast<unsigned char>(line[i]);
    if ((byte < 0x20 && byte != '\t') || byte == 0x7f) return false;
  }
  *status = static_cast<unsigned>(line[9] - '0') * 100 +
            static_cast<unsigned>(line[10] - '0') * 10 +
            static_cast<unsigned>(line[11] - '0');
  return *status >= 100 && *status <= 599;
}

inline bool parseContentLength(const char* value, uint32_t* length)
{
  if (!*value) return false;
  uint32_t parsed = 0;
  for (const char* current = value; *current; ++current) {
    if (!isdigit(static_cast<unsigned char>(*current))) return false;
    const unsigned digit = static_cast<unsigned>(*current - '0');
    if (parsed > (UINT32_MAX - digit) / 10) return false;
    parsed = parsed * 10 + digit;
  }
  *length = parsed;
  return true;
}

inline bool readBody(HTTPByteReader reader, void* readerContext,
                     HTTPBodyWriter writer, void* writerContext,
                     uint32_t length)
{
  unsigned char body[kBodyChunkCapacity];
  uint32_t remaining = length;
  while (remaining) {
    const size_t wanted = remaining < sizeof(body) ? remaining : sizeof(body);
    const int received = reader(readerContext, body, wanted);
    if (received <= 0 || static_cast<size_t>(received) > wanted ||
        !writer(writerContext, body, static_cast<size_t>(received))) return false;
    remaining -= static_cast<uint32_t>(received);
  }
  return true;
}

} // namespace detail

// Reads a bounded HTTP/1.0 or HTTP/1.1 response. A 200 response must have one
// valid Content-Length no larger than maxContentLength; only those body bytes
// are sent to writer. Non-200 response bodies are deliberately left unread so
// callers can close redirects/errors without passing them to a firmware sink.
inline bool streamHttpResponse(HTTPByteReader reader, void* readerContext,
                               uint32_t maxContentLength,
                               HTTPBodyWriter writer, void* writerContext,
                               HTTPResponseInfo* info)
{
  if (!reader || !writer || !info) return false;
  memset(info, 0, sizeof(*info));

  char line[detail::kHeaderLineCapacity];
  size_t headerBytes = 0;
  size_t lineLength = 0;
  if (!detail::readLine(reader, readerContext, line, sizeof(line), &lineLength)) {
    return false;
  }
  headerBytes = lineLength + 2;
  unsigned status = 0;
  if (headerBytes > detail::kHeaderBytesLimit ||
      !detail::validStatusLine(line, lineLength, &status)) return false;

  bool haveLength = false;
  bool haveLocation = false;
  bool haveTransferEncoding = false;
  uint32_t bodyLength = 0;
  bool ended = false;
  for (unsigned count = 0; count < detail::kHeaderLineLimit; ++count) {
    if (!detail::readLine(reader, readerContext, line, sizeof(line), &lineLength)) {
      return false;
    }
    headerBytes += lineLength + 2;
    if (headerBytes > detail::kHeaderBytesLimit) return false;
    if (!lineLength) { ended = true; break; }

    char* colon = strchr(line, ':');
    if (!colon || colon == line) return false;
    for (char* key = line; key < colon; ++key) {
      const unsigned char byte = static_cast<unsigned char>(*key);
      if (!(isalnum(byte) || strchr("!#$%&'*+-.^_`|~", byte))) return false;
    }
    for (const unsigned char* value =
             reinterpret_cast<const unsigned char*>(colon + 1);
         *value; ++value) {
      if ((*value < 0x20 && *value != '\t') || *value == 0x7f) return false;
    }

    const char* value = colon + 1;
    while (*value == ' ' || *value == '\t') ++value;
    char* valueEnd = line + lineLength;
    while (valueEnd > value &&
           (valueEnd[-1] == ' ' || valueEnd[-1] == '\t')) *--valueEnd = 0;
    const size_t keyLength = static_cast<size_t>(colon - line);
    if (keyLength == 14 && !strncasecmp(line, "Content-Length", keyLength)) {
      if (haveLength || !detail::parseContentLength(value, &bodyLength)) return false;
      if (status == 200 && bodyLength > maxContentLength) return false;
      haveLength = true;
    } else if (keyLength == 17 &&
               !strncasecmp(line, "Transfer-Encoding", keyLength)) {
      if (haveTransferEncoding || status == 200) return false;
      haveTransferEncoding = true;
    } else if (keyLength == 8 && !strncasecmp(line, "Location", keyLength)) {
      const size_t valueLength = strlen(value);
      if (haveLocation || valueLength >= sizeof(info->location)) return false;
      memcpy(info->location, value, valueLength + 1);
      haveLocation = true;
    }
  }
  if (!ended || (status == 200 && (!haveLength || haveTransferEncoding))) {
    return false;
  }
  if (!haveLength) bodyLength = 0;

  info->status = static_cast<uint16_t>(status);
  info->contentLength = bodyLength;
  if (status != 200) return true;
  return detail::readBody(reader, readerContext, writer, writerContext,
                          bodyLength);
}

} // namespace cell
} // namespace freematics
