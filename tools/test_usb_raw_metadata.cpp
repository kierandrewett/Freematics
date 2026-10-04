#include "../mode09_identity.h"
#include "../obd_raw_mode01.h"
#include "../usbtelemetry_metadata.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static void testRawWidths()
{
  assert(freematics::obd_raw::expectedBytes(0x01) == 4);
  assert(freematics::obd_raw::expectedBytes(0x02) == 2);
  assert(freematics::obd_raw::expectedBytes(0x03) == 2);
  for (uint8_t pid = 0x14; pid <= 0x1B; ++pid)
    assert(freematics::obd_raw::expectedBytes(pid) == 2);
  for (uint8_t pid = 0x24; pid <= 0x2B; ++pid)
    assert(freematics::obd_raw::expectedBytes(pid) == 4);
  for (uint8_t pid = 0x34; pid <= 0x3B; ++pid)
    assert(freematics::obd_raw::expectedBytes(pid) == 4);
  assert(freematics::obd_raw::expectedBytes(0x0C) == 0);

  const char* response[] = {"01 00 00 00", "AA BB", "01 02 03 04"};
  const uint8_t widths[] = {4, 2, 4};
  const uint8_t expected[][4] = {{1, 0, 0, 0}, {0xAA, 0xBB, 0, 0}, {1, 2, 3, 4}};
  for (size_t i = 0; i < 3; ++i) {
    uint8_t bytes[4] = {};
    assert(freematics::obd_raw::parseBytes(response[i], widths[i], bytes));
    assert(memcmp(bytes, expected[i], widths[i]) == 0);
  }
  uint8_t bytes[4] = {};
  assert(!freematics::obd_raw::parseBytes("01 02", 4, bytes));
  assert(!freematics::obd_raw::parseBytes("01 0Z", 2, bytes));
}

static void testCompleteMetadataCapacity()
{
  char metadata[USB_TELEMETRY_SUPPORT_SIZE] = {};
  size_t offset = 0;
  // Exercise an 87-entry support catalogue (the current firmware catalogue
  // maximum), retaining the legacy comma-separated list and suffix order.
  const uint8_t catalogue[] = {
#define OBD_PID(pid, name, description, unit, priority) pid,
#include "../obd_pids.h"
  };
  assert(sizeof(catalogue) == USB_TELEMETRY_RAW_PID_COUNT);
  for (uint8_t pid : catalogue)
    assert(appendSupportedMode01Pid(metadata, sizeof(metadata), offset, pid));
  assert(offset > 256);
  const char vin[] = ";vin=W0L0SDL68D4050841";
  memcpy(metadata + offset, vin, sizeof(vin));
  offset += sizeof(vin) - 1;
  assert(freematics::mode09::appendHexMetadata(metadata, sizeof(metadata), offset,
                                                "cal", "CAL-ID-12345678"));
  assert(freematics::mode09::appendHexMetadata(metadata, sizeof(metadata), offset,
                                                "ecu", "ECU-NAME-1234567890"));

  UsbRawMode01Value raw[USB_TELEMETRY_RAW_PID_COUNT] = {};
  uint8_t index = 0;
  for (uint8_t pid : catalogue) {
    raw[index].pid = pid;
    // Worst case bounds metadata even if each catalogue entry returns four bytes.
    raw[index].length = 4;
    raw[index].valid = 1;
    for (uint8_t byte = 0; byte < raw[index].length; ++byte)
      raw[index].bytes[byte] = (uint8_t)(index + byte);
    ++index;
  }
  assert(appendRawMode01Metadata(metadata, sizeof(metadata), offset, raw,
                                 USB_TELEMETRY_RAW_PID_COUNT));
  metadata[offset] = 0;
  assert(strstr(metadata, ";vin=W0L0SDL68D4050841;cal=43414C2D49442D3132333435363738;ecu=4543552D4E414D452D31323334353637383930;raw=") != nullptr);
  assert(strstr(metadata, "01:00010203") != nullptr);
  assert(strstr(metadata, "14:") != nullptr);
  assert(strstr(metadata, "24:") != nullptr);
  assert(strstr(metadata, "3B:") != nullptr);
  assert(strstr(metadata, "A6:") != nullptr);
  assert(offset < sizeof(metadata));
}

static void testOverflowIsReported()
{
  UsbRawMode01Value raw = {0x01, 4, {0, 1, 2, 3}, 1};
  char tiny[12] = "support";
  size_t offset = strlen(tiny);
  assert(!appendRawMode01Metadata(tiny, sizeof(tiny), offset, &raw, 1));
}

int main()
{
  testRawWidths();
  testCompleteMetadataCapacity();
  testOverflowIsReported();
  puts("USB raw Mode 01 metadata tests passed");
}
