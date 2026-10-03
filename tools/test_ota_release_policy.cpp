#include "../ota_release_policy.h"

#include <assert.h>
#include <string.h>

namespace {

void testAllowsOnlyKnownHttpsHosts()
{
  const char* locations[] = {
      "https://github.com/kierandrewett/Freematics/releases/latest/download/a.bin",
      "https://release-assets.githubusercontent.com/github-production-release-asset/a.bin?x=1",
      "https://github-releases.githubusercontent.com/release/a.bin",
      "https://objects.githubusercontent.com/release/a.bin",
  };
  for (const char* location : locations) {
    char host[128] = {};
    char path[512] = {};
    assert(freematics::ota::parseHttpsReleaseLocation(
        location, host, sizeof(host), path, sizeof(path)));
    assert(freematics::ota::allowedReleaseHost(host));
    assert(path[0] == '/' && path[1] != '/');
  }
}

void testRejectsUnsafeAuthoritiesAndPaths()
{
  const char* locations[] = {
      "http://github.com/release/a.bin",
      "https://github.com.evil.example/release/a.bin",
      "https://github.com@evil.example/release/a.bin",
      "https://github.com:443/release/a.bin",
      "https://evil.example@github.com/release/a.bin",
      "https://github.com//evil.example/a.bin",
      "https://github.com/release/a.bin#fragment",
      "https://github.com/release/a.bin\r\nHost: evil.example",
      "https://github.com",
      "//github.com/release/a.bin",
  };
  for (const char* location : locations) {
    char host[128] = {};
    char path[512] = {};
    assert(!freematics::ota::parseHttpsReleaseLocation(
        location, host, sizeof(host), path, sizeof(path)));
  }
}

void testRejectsTruncation()
{
  char host[8] = {};
  char path[512] = {};
  assert(!freematics::ota::parseHttpsReleaseLocation(
      "https://github.com/a", host, sizeof(host), path, sizeof(path)));

  char shortHost[128] = {};
  char shortPath[4] = {};
  assert(!freematics::ota::parseHttpsReleaseLocation(
      "https://github.com/long", shortHost, sizeof(shortHost),
      shortPath, sizeof(shortPath)));
}

} // namespace

int main()
{
  testAllowsOnlyKnownHttpsHosts();
  testRejectsUnsafeAuthoritiesAndPaths();
  testRejectsTruncation();
  return 0;
}
