#pragma once

#include <stddef.h>
#include <string.h>

namespace freematics {
namespace ota {

inline bool allowedReleaseHost(const char* host)
{
  return host && (!strcmp(host, "github.com") ||
      !strcmp(host, "release-assets.githubusercontent.com") ||
      !strcmp(host, "github-releases.githubusercontent.com") ||
      !strcmp(host, "objects.githubusercontent.com"));
}

// Accept only HTTPS URLs on the fixed GitHub release-asset host allowlist.
// Writes an origin-form request target, never a proxy/absolute URL.
inline bool parseHttpsReleaseLocation(const char* location,
                                      char* host, size_t hostCapacity,
                                      char* path, size_t pathCapacity)
{
  static const char prefix[] = "https://";
  if (!location || !host || !path ||
      strncmp(location, prefix, sizeof(prefix) - 1)) return false;

  const char* authority = location + sizeof(prefix) - 1;
  const char* slash = strchr(authority, '/');
  if (!slash || slash == authority || slash[1] == '/') return false;
  const size_t hostLength = (size_t)(slash - authority);
  if (hostLength >= hostCapacity || strlen(slash) >= pathCapacity ||
      memchr(authority, '@', hostLength) || memchr(authority, ':', hostLength)) return false;

  memcpy(host, authority, hostLength);
  host[hostLength] = 0;
  if (!allowedReleaseHost(host)) return false;

  for (const char* p = slash; *p; ++p) {
    const unsigned char ch = (unsigned char)*p;
    if (ch <= 0x20 || ch == 0x7f || ch == '\\' || ch == '"' || ch == '#') return false;
  }
  strcpy(path, slash);
  return true;
}

} // namespace ota
} // namespace freematics
