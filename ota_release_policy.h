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

// Recover the immutable release tag from GitHub's redirect target for a
// /releases/latest/download request. Asset URLs on the CDN no longer carry
// this route, so capture it before following the redirect.
inline bool parseReleaseTagFromDownloadPath(const char* path,
                                            const char* expectedAsset,
                                            char* tag, size_t tagCapacity)
{
  static const char prefix[] =
      "/kierandrewett/Freematics/releases/download/";
  if (!path || !expectedAsset || !tag || !tagCapacity ||
      strncmp(path, prefix, sizeof(prefix) - 1)) return false;

  const char* tagStart = path + sizeof(prefix) - 1;
  const char* slash = strchr(tagStart, '/');
  if (!slash || slash == tagStart) return false;
  const size_t tagLength = (size_t)(slash - tagStart);
  if (tagLength >= tagCapacity || tagLength > 64) return false;
  if (!((tagStart[0] >= 'A' && tagStart[0] <= 'Z') ||
        (tagStart[0] >= 'a' && tagStart[0] <= 'z') ||
        (tagStart[0] >= '0' && tagStart[0] <= '9'))) return false;
  for (size_t i = 1; i < tagLength; ++i) {
    const char ch = tagStart[i];
    if (!((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') ||
          (ch >= '0' && ch <= '9') || ch == '.' || ch == '_' || ch == '-'))
      return false;
  }
  if (tagLength == 6 && !memcmp(tagStart, "latest", 6)) return false;

  const size_t assetLength = strlen(expectedAsset);
  const char* assetStart = slash + 1;
  if (strlen(assetStart) < assetLength ||
      memcmp(assetStart, expectedAsset, assetLength)) return false;
  const char suffix = assetStart[assetLength];
  if (suffix != 0 && suffix != '?') return false;

  memcpy(tag, tagStart, tagLength);
  tag[tagLength] = 0;
  return true;
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
