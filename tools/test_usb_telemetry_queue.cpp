#include "../usbtelemetry.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static void publish(UsbTelemetryQueue& queue, const char* text)
{
  UsbTelemetryRecord* record = queue.reserve();
  assert(record);
  const size_t length = strlen(text);
  assert(length < sizeof(record->payload));
  memcpy(record->payload, text, length);
  assert(queue.publish(record, static_cast<uint16_t>(length)));
}

static void testSlowReaderKeepsOnlyLatestWaitingRecord()
{
  UsbTelemetryQueue queue;
  publish(queue, "in-flight");
  UsbTelemetryRecord* inFlight = queue.peek();
  assert(inFlight && strcmp(inFlight->payload, "in-flight") == 0);

  publish(queue, "old-1");
  publish(queue, "old-2");
  publish(queue, "old-3");
  UsbTelemetryRecord* reserved = queue.reserve();
  assert(reserved);
  memcpy(reserved->payload, "latest", sizeof("latest"));
  assert(queue.publish(reserved, sizeof("latest") - 1));

  assert(strcmp(inFlight->payload, "in-flight") == 0);
  assert(queue.peek() == nullptr); // only one UART writer may own the stream
  assert(queue.dropped() == 3);

  queue.release();
  UsbTelemetryRecord* latest = queue.peek();
  assert(latest && strcmp(latest->payload, "latest") == 0);
  queue.release();
}

static void testNewerSnapshotReplacesWaitingSnapshot()
{
  UsbTelemetryQueue queue;
  publish(queue, "one");
  assert(queue.dropped() == 0);
  publish(queue, "two");
  assert(queue.dropped() == 1);
  publish(queue, "three");
  assert(queue.dropped() == 2);
  UsbTelemetryRecord* newest = queue.peek();
  assert(newest && strcmp(newest->payload, "three") == 0);
  queue.release();
}

int main()
{
  testSlowReaderKeepsOnlyLatestWaitingRecord();
  testNewerSnapshotReplacesWaitingSnapshot();
  puts("USB telemetry queue: bounded backpressure and drop accounting passed");
  return 0;
}
