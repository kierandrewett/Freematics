#include "../telemetry_endpoint_policy.h"

#include <assert.h>
#include <string.h>

int main()
{
  using freematics::endpoint::copyString;
  using freematics::endpoint::kRequirePrivateMigration;
  using freematics::endpoint::kSeedPrivateMigrationMarker;
  using freematics::endpoint::kUseMigratedPrivateConfig;
  using freematics::endpoint::kBuildSeedValue;
  using freematics::endpoint::kInvalidValue;
  using freematics::endpoint::kStoredValue;
  using freematics::endpoint::resolveConfigMarker;
  using freematics::endpoint::resolveStoredOrSeed;
  using freematics::endpoint::resolveValue;
  using freematics::endpoint::validHost;
  using freematics::endpoint::validPath;

  assert(validHost("freematics.drewett.dev"));
  assert(validHost("192.0.2.1"));
  assert(!validHost(""));
  assert(!validHost("-invalid.example"));
  assert(!validHost("invalid-.example"));
  assert(!validHost("invalid..example"));
  assert(!validHost("host.example/path"));
  assert(!validHost("host.example:443"));

  assert(validPath("/api"));
  assert(validPath("/api/v1/upload?source=device"));
  assert(validPath("/api/%41"));
  assert(!validPath(""));
  assert(!validPath("api"));
  assert(!validPath("/api with-space"));
  assert(!validPath("/api/%Q0"));
  assert(!validPath("/api#fragment"));
  assert(!validPath("/api\\unsafe"));

  char destination[8] = {};
  assert(copyString(destination, sizeof(destination), "safe"));
  assert(strcmp(destination, "safe") == 0);
  assert(!copyString(destination, sizeof(destination), "12345678"));
  assert(!copyString(destination, sizeof(destination), "too-long"));
  assert(!copyString(destination, sizeof(destination), nullptr));

  char host[32] = {};
  assert(resolveValue(nullptr, false, "bootstrap.example", true,
      true, host, sizeof(host)) == kBuildSeedValue);
  assert(strcmp(host, "bootstrap.example") == 0);
  assert(resolveValue("stored.example", true, "bootstrap.example", true,
      true, host, sizeof(host)) == kStoredValue);
  assert(strcmp(host, "stored.example") == 0);
  assert(resolveValue(nullptr, false, "bootstrap.example", false,
      true, host, sizeof(host)) == kInvalidValue);
  assert(resolveValue("bad/path", true, "bootstrap.example", true,
      true, host, sizeof(host)) == kInvalidValue);
  assert(host[0] == 0);

  char path[32] = {};
  assert(resolveValue("/persisted/api", true, "/bootstrap", false,
      false, path, sizeof(path)) == kStoredValue);
  assert(strcmp(path, "/persisted/api") == 0);

  char optionalConfig[16] = {};
  assert(resolveStoredOrSeed(nullptr, false, "", true, optionalConfig,
      sizeof(optionalConfig)) == kBuildSeedValue);
  assert(optionalConfig[0] == 0);
  assert(resolveStoredOrSeed(nullptr, false, "private", false, optionalConfig,
      sizeof(optionalConfig)) == kInvalidValue);
  assert(resolveStoredOrSeed("", true, "fallback", true, optionalConfig,
      sizeof(optionalConfig)) == kStoredValue);
  assert(optionalConfig[0] == 0);
  assert(resolveStoredOrSeed(nullptr, false, "", true, optionalConfig,
      sizeof(optionalConfig), true, true) == kInvalidValue);
  assert(resolveStoredOrSeed("", true, "", true, optionalConfig,
      sizeof(optionalConfig), true, true) == kInvalidValue);
  assert(resolveStoredOrSeed("persisted-apn", true, "", false,
      optionalConfig, sizeof(optionalConfig), true, true) == kStoredValue);
  assert(strcmp(optionalConfig, "persisted-apn") == 0);
  assert(resolveStoredOrSeed("", true, "", false, optionalConfig,
      sizeof(optionalConfig), true, false) == kStoredValue);
  assert(optionalConfig[0] == 0);

  assert(resolveConfigMarker(false, 0, true, true) == kSeedPrivateMigrationMarker);
  assert(resolveConfigMarker(false, 0, false, true) == kRequirePrivateMigration);
  assert(resolveConfigMarker(true, 1, false, true) == kUseMigratedPrivateConfig);
  assert(resolveConfigMarker(true, 2, true, true) == kRequirePrivateMigration);
  assert(resolveConfigMarker(true, 1, false, false) == kRequirePrivateMigration);
  return 0;
}
