#include "../ota_sha256_sidecar.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

namespace {

const char kPackage[] = "freematics-model-b.bin";
const char kSidecar[] = "freematics-model-b.bin.sha256sum";
const char kDigestHex[] =
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

void makeDigest(uint8_t digest[32])
{
    for (size_t i = 0; i < 32; ++i)
        digest[i] = static_cast<uint8_t>(
            (freematics::ota::hexValue(kDigestHex[i * 2]) << 4) |
            freematics::ota::hexValue(kDigestHex[i * 2 + 1]));
}

bool validate(const char* package, const char* sidecar,
              const char* content, const uint8_t digest[32])
{
    return freematics::ota::validateSha256Sidecar(
        package, sidecar, content, strlen(content), digest);
}

void testValidFormatsAndHexCase()
{
    uint8_t digest[32];
    makeDigest(digest);

    char content[160];
    snprintf(content, sizeof(content), "%s  %s\n", kDigestHex, kPackage);
    assert(validate(kPackage, kSidecar, content, digest));
    uint8_t parsed[32] = {};
    assert(freematics::ota::parseSha256Sidecar(
        kPackage, kSidecar, content, strlen(content), parsed));
    assert(freematics::ota::constantTimeDigestEqual(parsed, digest));

    snprintf(content, sizeof(content), "%s *%s", kDigestHex, kPackage);
    assert(validate(kPackage, kSidecar, content, digest));

    char uppercase[sizeof(kDigestHex)];
    for (size_t i = 0; i < sizeof(kDigestHex); ++i) {
        const char c = kDigestHex[i];
        uppercase[i] = c >= 'a' && c <= 'f' ? static_cast<char>(c - 'a' + 'A') : c;
    }
    snprintf(content, sizeof(content), "%s\t*%s\r\n", uppercase, kPackage);
    assert(validate(kPackage, kSidecar, content, digest));

    assert(freematics::ota::hasSha256SidecarName(
        "/tmp/releases/freematics-model-b.bin",
        "/tmp/releases/freematics-model-b.bin.sha256sum"));
}

void testFilenameAndFormatRejections()
{
    uint8_t digest[32];
    makeDigest(digest);

    char content[160];
    snprintf(content, sizeof(content), "%s  %s\n", kDigestHex, kPackage);
    assert(!validate(kPackage, "wrong.bin.sha256sum", content, digest));
    assert(!validate("other.bin", kSidecar, content, digest));

    snprintf(content, sizeof(content), "%.63s  %s", kDigestHex, kPackage);
    assert(!validate(kPackage, kSidecar, content, digest));

    snprintf(content, sizeof(content), "%s%s  %s", kDigestHex, "0", kPackage);
    assert(!validate(kPackage, kSidecar, content, digest));

    snprintf(content, sizeof(content), "%s  %s", kDigestHex, kPackage);
    content[20] = 'g';
    assert(!validate(kPackage, kSidecar, content, digest));

    snprintf(content, sizeof(content), "%s  other.bin", kDigestHex);
    assert(!validate(kPackage, kSidecar, content, digest));
    assert(!freematics::ota::parseSha256Sidecar(
        kPackage, kSidecar, content, strlen(content), digest));

    snprintf(content, sizeof(content), "%s  %s extra", kDigestHex, kPackage);
    assert(!validate(kPackage, kSidecar, content, digest));

    snprintf(content, sizeof(content), "%s%s  %s", kDigestHex, "x", kPackage);
    assert(!validate(kPackage, kSidecar, content, digest));

    snprintf(content, sizeof(content), "%s%s", kDigestHex, "  ");
    assert(!validate(kPackage, kSidecar, content, digest));
}

void testDigestComparison()
{
    uint8_t digest[32];
    uint8_t other[32];
    makeDigest(digest);
    memcpy(other, digest, sizeof(digest));

    assert(freematics::ota::constantTimeDigestEqual(digest, other));
    other[0] ^= 1;
    assert(!freematics::ota::constantTimeDigestEqual(digest, other));
    memcpy(other, digest, sizeof(digest));
    other[31] ^= 1;
    assert(!freematics::ota::constantTimeDigestEqual(digest, other));

    char content[160];
    snprintf(content, sizeof(content), "%s  %s", kDigestHex, kPackage);
    other[0] ^= 1;
    assert(!validate(kPackage, kSidecar, content, other));
}

} // namespace

int main()
{
    testValidFormatsAndHexCase();
    testFilenameAndFormatRejections();
    testDigestComparison();
    puts("OTA SHA-256 sidecar: all tests passed");
    return 0;
}
