#include "../mode09_identity.h"

#include <assert.h>
#include <string.h>

int main()
{
  uint8_t bytes[64] = {};
  size_t length = 0;
  assert(freematics::mode09::parseHexResponse(
      "SEARCHING...\r\n49 00 10 40 00 00\r\n>", 0x00,
      bytes, sizeof(bytes), length));
  assert(length == 6);
  assert(freematics::mode09::supports(bytes, length, 0x04));
  assert(!freematics::mode09::supports(bytes, length, 0x02));
  assert(freematics::mode09::supports(bytes, length, 0x0A));
  assert(!freematics::mode09::supports(bytes, length, 0x21));

  char output[21] = {};
  const uint8_t calibration[] = {
      0x49, 0x04, 0x01, 'C', 'A', 'L', '-', 'I', 'D', '-', '1', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ',
  };
  assert(freematics::mode09::parseTextRecord(calibration, sizeof(calibration),
                                             0x04, 16, output, sizeof(output)));
  assert(strcmp(output, "CAL-ID-1") == 0);

  const uint8_t ecuName[] = {
      0x49, 0x0A, 0x01, 'E', 'N', 'G', 'I', 'N', 'E', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ',
  };
  assert(freematics::mode09::parseTextRecord(ecuName, sizeof(ecuName),
                                             0x0A, 20, output, sizeof(output)));
  assert(strcmp(output, "ENGINE") == 0);

  assert(!freematics::mode09::parseHexResponse("490141", 0x04,
                                               bytes, sizeof(bytes), length));
  assert(freematics::mode09::parseHexResponse("490400", 0x04,
                                              bytes, sizeof(bytes), length));
  assert(!freematics::mode09::parseTextRecord(bytes, length, 0x04, 16,
                                               output, sizeof(output)));
  assert(!freematics::mode09::parseTextRecord(calibration, 8, 0x04, 16,
                                              output, sizeof(output)));
  uint8_t badCalibration[sizeof(calibration)];
  memcpy(badCalibration, calibration, sizeof(calibration));
  badCalibration[3] = 0x01;
  assert(!freematics::mode09::parseTextRecord(badCalibration, sizeof(badCalibration),
                                              0x04, 16, output, sizeof(output)));
  char metadata[64] = "0C,0D;vin=1HGCM82633A004352";
  size_t metadataLength = strlen(metadata);
  assert(freematics::mode09::appendHexMetadata(metadata, sizeof(metadata),
                                               metadataLength, "cal", "CAL-ID-1"));
  assert(strcmp(metadata,
      "0C,0D;vin=1HGCM82633A004352;cal=43414C2D49442D31") == 0);
  char tooSmall[8] = "0C";
  size_t tooSmallLength = strlen(tooSmall);
  assert(!freematics::mode09::appendHexMetadata(tooSmall, sizeof(tooSmall),
                                                tooSmallLength, "cal", "CAL-ID-1"));
  assert(strcmp(tooSmall, "0C") == 0);
  return 0;
}
