#include "../lib/FreematicsPlus/FreematicsHttpStream.h"

#include <assert.h>
#include <string.h>

namespace {

struct Input {
  const unsigned char* bytes;
  size_t length;
  size_t offset;
  size_t maxChunk;
};

int readInput(void* context, unsigned char* output, size_t capacity)
{
  Input* input = static_cast<Input*>(context);
  if (input->offset == input->length) return 0;
  size_t count = input->length - input->offset;
  if (count > input->maxChunk) count = input->maxChunk;
  if (count > capacity) count = capacity;
  memcpy(output, input->bytes + input->offset, count);
  input->offset += count;
  return static_cast<int>(count);
}

struct Output {
  unsigned char bytes[64];
  size_t length;
  unsigned calls;
  bool reject;
};

bool writeOutput(void* context, const unsigned char* bytes, size_t length)
{
  Output* output = static_cast<Output*>(context);
  if (output->reject) return false;
  if (length > sizeof(output->bytes) - output->length) return false;
  memcpy(output->bytes + output->length, bytes, length);
  output->length += length;
  output->calls++;
  return true;
}

bool stream(const char* response, size_t readChunk, uint32_t maxLength,
            freematics::cell::HTTPResponseInfo* info, Output* output)
{
  Input input = {reinterpret_cast<const unsigned char*>(response),
                 strlen(response), 0, readChunk};
  return freematics::cell::streamHttpResponse(
      readInput, &input, maxLength, writeOutput, output, info);
}

void testStreamsFixedLengthBodyAcrossPartialReads()
{
  const char response[] =
      "HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello";
  freematics::cell::HTTPResponseInfo info = {};
  Output output = {};
  assert(stream(response, 1, 5, &info, &output));
  assert(info.status == 200);
  assert(info.contentLength == 5);
  assert(output.length == 5);
  assert(memcmp(output.bytes, "hello", 5) == 0);
}

void testRejectsTruncatedBody()
{
  const char response[] =
      "HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhe";
  freematics::cell::HTTPResponseInfo info = {};
  Output output = {};
  assert(!stream(response, 2, 5, &info, &output));
}

void testRedirectMetadataNeverReachesFirmwareWriter()
{
  const char response[] =
      "HTTP/1.1 302 Found\r\nLocation: https://github.com/release/image\r\n"
      "Content-Length: 6\r\n\r\nsecret";
  freematics::cell::HTTPResponseInfo info = {};
  Output output = {};
  Input input = {reinterpret_cast<const unsigned char*>(response),
                 sizeof(response) - 1, 0, 3};
  assert(freematics::cell::streamHttpResponse(
      readInput, &input, 5, writeOutput, &output, &info));
  assert(info.status == 302);
  assert(info.contentLength == 6);
  assert(strcmp(info.location, "https://github.com/release/image") == 0);
  assert(output.calls == 0);
  assert(input.offset < input.length);
}

void testRejectsInvalidContentLengthAndFraming()
{
  const char* responses[] = {
      "HTTP/1.1 200 OK\r\n\r\n",
      "HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx",
      "HTTP/1.1 200 OK\r\nContent-Length: +1\r\n\r\nx",
      "HTTP/1.1 200 OK\r\nContent-Length: 4294967296\r\n\r\nx",
      "HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n123456",
      "HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\nx",
  };
  for (size_t index = 0; index < sizeof(responses) / sizeof(responses[0]); ++index) {
    freematics::cell::HTTPResponseInfo info = {};
    Output output = {};
    assert(!stream(responses[index], 4, 5, &info, &output));
  }
}

void testRejectsMalformedStatusAndHeaderSyntax()
{
  const char* responses[] = {
      "HTTP/2 200 OK\r\nContent-Length: 0\r\n\r\n",
      "HTTP/1.1 099 Bad\r\nContent-Length: 0\r\n\r\n",
      "HTTP/1.1 200 OK\r\n folded: value\r\nContent-Length: 0\r\n\r\n",
      "HTTP/1.1 200 OK\r\nContent Length: 0\r\n\r\n",
      "HTTP/1.1 200 OK\nContent-Length: 0\r\n\r\n",
  };
  for (size_t index = 0; index < sizeof(responses) / sizeof(responses[0]); ++index) {
    freematics::cell::HTTPResponseInfo info = {};
    Output output = {};
    assert(!stream(responses[index], 7, 8, &info, &output));
  }
}

void testStopsWhenFirmwareWriterRejectsBody()
{
  const char response[] =
      "HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello";
  freematics::cell::HTTPResponseInfo info = {};
  Output output = {};
  output.reject = true;
  assert(!stream(response, 5, 5, &info, &output));
  assert(output.calls == 0);
}

} // namespace

int main()
{
  testStreamsFixedLengthBodyAcrossPartialReads();
  testRejectsTruncatedBody();
  testRedirectMetadataNeverReachesFirmwareWriter();
  testRejectsInvalidContentLengthAndFraming();
  testRejectsMalformedStatusAndHeaderSyntax();
  testStopsWhenFirmwareWriterRejectsBody();
  return 0;
}
