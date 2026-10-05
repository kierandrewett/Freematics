#define _POSIX_C_SOURCE 200809L
#include "capture_inbox.h"

#ifdef _WIN32

CaptureInboxResult captureInboxStore(const char* root, const char* device_id,
                                     uint64_t session, uint32_t sequence,
                                     const void* payload, size_t length)
{
    (void)root;
    (void)device_id;
    (void)session;
    (void)sequence;
    (void)payload;
    (void)length;
    return CAPTURE_INBOX_ERROR;
}

CaptureInboxResult captureInboxReceiptEnsure(const char* root, const char* device_id,
                                             uint64_t session, uint32_t sequence,
                                             int64_t now_seconds,
                                             CaptureInboxReceipt* receipt_out)
{
    (void)root; (void)device_id; (void)session; (void)sequence;
    (void)now_seconds; (void)receipt_out;
    return CAPTURE_INBOX_ERROR;
}

#else

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

#define INBOX_DIR "capture-inbox"
#define HEADER_LIMIT 160

static int valid_device_id(const char* id);
static int ensure_directory(const char* path);
static int write_all(int fd, const void* data, size_t length);
static int read_exact(int fd, void* data, size_t length);

static uint32_t crc32_bytes(const unsigned char* data, size_t length)
{
    uint32_t crc = UINT32_C(0xffffffff);
    size_t i;
    for (i = 0; i < length; ++i) {
        unsigned bit;
        crc ^= data[i];
        for (bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ ((crc & 1U) ? UINT32_C(0xedb88320) : 0U);
    }
    return crc ^ UINT32_C(0xffffffff);
}

#define RECEIPT_MIN_EPOCH_MS UINT64_C(1704067200000)
#define RECEIPT_MAX_EPOCH_MS UINT64_C(4102444800000)

static int make_receipt(uint64_t session, uint32_t sequence, int64_t now_seconds,
                        char* record, size_t capacity, CaptureInboxReceipt* receipt)
{
    char prefix[128];
    uint64_t epoch_ms = 0;
    int plausible = 0;
    int prefix_length, record_length;
    uint32_t crc;
    if (now_seconds >= 0 && (uint64_t)now_seconds <= UINT64_MAX / 1000U) {
        uint64_t candidate = (uint64_t)now_seconds * 1000U;
        if (candidate >= RECEIPT_MIN_EPOCH_MS && candidate <= RECEIPT_MAX_EPOCH_MS) {
            epoch_ms = candidate;
            plausible = 1;
        }
    }
    prefix_length = snprintf(prefix, sizeof(prefix), "FQR1,%016" PRIx64 ",%" PRIu32 ",%" PRIu64 ",%d",
                             session, sequence, epoch_ms, plausible);
    if (prefix_length < 0 || (size_t)prefix_length >= sizeof(prefix)) return 0;
    crc = crc32_bytes((const unsigned char*)prefix, (size_t)prefix_length);
    record_length = snprintf(record, capacity, "%s,%08" PRIx32 "\n", prefix, crc);
    if (record_length < 0 || (size_t)record_length >= capacity) return 0;
    if (receipt) {
        receipt->epoch_ms = epoch_ms;
        receipt->clock_plausible = plausible;
    }
    return record_length;
}

static int read_receipt(const char* path, uint64_t session, uint32_t sequence,
                        CaptureInboxReceipt* receipt)
{
    char record[160], expected[160];
    ssize_t count;
    int fd = open(path, O_RDONLY | O_NOFOLLOW);
    struct stat st;
    int64_t epoch_seconds;
    int consumed = 0;
    uint64_t stored_session, epoch_ms;
    uint32_t stored_sequence, stored_crc;
    int plausible;
    int expected_length;
    if (fd < 0) return -1;
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size <= 0 ||
        st.st_size >= (off_t)sizeof(record)) { close(fd); return -1; }
    count = st.st_size;
    if (!read_exact(fd, record, (size_t)count) || fsync(fd) != 0) { close(fd); return -1; }
    {
        unsigned char extra;
        ssize_t n;
        do { n = read(fd, &extra, 1); } while (n < 0 && errno == EINTR);
        if (n != 0) { close(fd); return -1; }
    }
    if (close(fd) != 0) return -1;
    record[count] = '\0';
    if (sscanf(record, "FQR1,%16" SCNx64 ",%" SCNu32 ",%" SCNu64 ",%d,%8" SCNx32 "\n%n",
               &stored_session, &stored_sequence, &epoch_ms, &plausible, &stored_crc, &consumed) != 5 ||
        consumed != count || (size_t)count != strlen(record) ||
        stored_session != session || stored_sequence != sequence ||
        (plausible != 0 && plausible != 1)) return -1;
    if ((!plausible && epoch_ms != 0) ||
        (plausible && (epoch_ms < RECEIPT_MIN_EPOCH_MS || epoch_ms > RECEIPT_MAX_EPOCH_MS || epoch_ms % 1000U)))
        return -1;
    epoch_seconds = plausible ? (int64_t)(epoch_ms / 1000U) : 0;
    expected_length = make_receipt(session, sequence, epoch_seconds, expected, sizeof(expected), NULL);
    if (expected_length != count || memcmp(expected, record, (size_t)count) != 0) return -1;
    if (receipt) {
        receipt->epoch_ms = epoch_ms;
        receipt->clock_plausible = plausible;
    }
    return 1;
}

CaptureInboxResult captureInboxReceiptEnsure(const char* root, const char* device_id,
                                             uint64_t session, uint32_t sequence,
                                             int64_t now_seconds,
                                             CaptureInboxReceipt* receipt_out)
{
    char inbox[PATH_MAX], device[PATH_MAX], final_path[PATH_MAX];
    char temp_path[PATH_MAX], record[160], filename[96], capture_filename[96];
    CaptureInboxReceipt candidate;
    int dirfd = -1, fd = -1, record_length, existing;
    struct stat st;
    if (receipt_out) memset(receipt_out, 0, sizeof(*receipt_out));
    if (!root || !*root || !valid_device_id(device_id) || !session || strlen(root) >= PATH_MAX - 160)
        return CAPTURE_INBOX_INVALID;
    if (snprintf(inbox, sizeof(inbox), "%s/%s", root, INBOX_DIR) >= (int)sizeof(inbox) ||
        snprintf(device, sizeof(device), "%s/%s", inbox, device_id) >= (int)sizeof(device) ||
        snprintf(capture_filename, sizeof(capture_filename), "%016" PRIx64 "-%" PRIu32 ".fqi", session, sequence) >= (int)sizeof(capture_filename) ||
        snprintf(filename, sizeof(filename), "%016" PRIx64 "-%" PRIu32 ".receipt", session, sequence) >= (int)sizeof(filename) ||
        snprintf(final_path, sizeof(final_path), "%s/%s", device, filename) >= (int)sizeof(final_path))
        return CAPTURE_INBOX_INVALID;
    if (!ensure_directory(root) || !ensure_directory(inbox) || !ensure_directory(device)) return CAPTURE_INBOX_ERROR;
    dirfd = open(device, O_RDONLY | O_DIRECTORY);
    if (dirfd < 0) return CAPTURE_INBOX_ERROR;
    if (fstatat(dirfd, capture_filename, &st, AT_SYMLINK_NOFOLLOW) != 0) {
        int saved_errno = errno;
        close(dirfd);
        return saved_errno == ENOENT ? CAPTURE_INBOX_INVALID : CAPTURE_INBOX_ERROR;
    }
    if (!S_ISREG(st.st_mode)) { close(dirfd); return CAPTURE_INBOX_INVALID; }
    if (fstatat(dirfd, filename, &st, AT_SYMLINK_NOFOLLOW) == 0) {
        if (!S_ISREG(st.st_mode)) { close(dirfd); return CAPTURE_INBOX_ERROR; }
        existing = read_receipt(final_path, session, sequence, receipt_out);
        if (existing != 1) { close(dirfd); return CAPTURE_INBOX_ERROR; }
    } else if (errno != ENOENT) {
        close(dirfd);
        return CAPTURE_INBOX_ERROR;
    } else {
        existing = 0;
    }
    if (existing == 1) {
        int sync_ok = fsync(dirfd) == 0;
        int close_ok = close(dirfd) == 0;
        return sync_ok && close_ok ? CAPTURE_INBOX_DUPLICATE : CAPTURE_INBOX_ERROR;
    }
    record_length = make_receipt(session, sequence, now_seconds, record, sizeof(record), &candidate);
    if (!record_length) { close(dirfd); return CAPTURE_INBOX_ERROR; }
    if (snprintf(temp_path, sizeof(temp_path), "%s/.receipt-%ld-XXXXXX", device, (long)getpid()) >= (int)sizeof(temp_path)) {
        close(dirfd); return CAPTURE_INBOX_ERROR;
    }
    fd = mkstemp(temp_path);
    if (fd < 0) { close(dirfd); return CAPTURE_INBOX_ERROR; }
    (void)fchmod(fd, 0600);
    if (!write_all(fd, record, (size_t)record_length) || fsync(fd) != 0) {
        close(fd); unlink(temp_path); close(dirfd); return CAPTURE_INBOX_ERROR;
    }
    if (close(fd) != 0) { unlink(temp_path); close(dirfd); return CAPTURE_INBOX_ERROR; }
    fd = -1;
    if (link(temp_path, final_path) != 0) {
        int saved_errno = errno;
        unlink(temp_path);
        if (saved_errno == EEXIST) {
            existing = read_receipt(final_path, session, sequence, receipt_out);
            if (existing == 1) {
                int sync_ok = fsync(dirfd) == 0;
                int close_ok = close(dirfd) == 0;
                if (sync_ok && close_ok) return CAPTURE_INBOX_DUPLICATE;
                return CAPTURE_INBOX_ERROR;
            }
        }
        close(dirfd);
        return CAPTURE_INBOX_ERROR;
    }
    if (fsync(dirfd) != 0) { unlink(temp_path); close(dirfd); return CAPTURE_INBOX_ERROR; }
    if (unlink(temp_path) != 0) { close(dirfd); return CAPTURE_INBOX_ERROR; }
    {
        int sync_ok = fsync(dirfd) == 0;
        int close_ok = close(dirfd) == 0;
        if (!sync_ok || !close_ok) return CAPTURE_INBOX_ERROR;
    }
    if (receipt_out) *receipt_out = candidate;
    return CAPTURE_INBOX_STORED;
}

static int valid_device_id(const char* id)
{
    size_t i, n;
    if (!id || !(n = strlen(id)) || n > 128 || id[0] == '.') return 0;
    for (i = 0; i < n; ++i) {
        unsigned char c = (unsigned char)id[i];
        if (!(isalnum(c) || c == '-' || c == '_' || c == '.')) return 0;
    }
    return 1;
}

/* Create path components one at a time; sync each parent after a new entry. */
static int ensure_directory(const char* path)
{
    char current[PATH_MAX];
    size_t i, n;
    if (!path || !(n = strlen(path)) || n >= sizeof(current)) return 0;
    memcpy(current, path, n + 1);
    for (i = 1; i <= n; ++i) {
        if (current[i] != '/' && current[i] != '\0') continue;
        {
            char saved = current[i];
            struct stat st;
            current[i] = '\0';
            if (current[0] && stat(current, &st) != 0) {
                if (errno != ENOENT || mkdir(current, 0700) != 0) {
                    if (errno != EEXIST || stat(current, &st) != 0 || !S_ISDIR(st.st_mode))
                        return 0;
                } else {
                    char parent[PATH_MAX];
                    char* slash;
                    int fd;
                    memcpy(parent, current, strlen(current) + 1);
                    slash = strrchr(parent, '/');
                    if (slash) {
                        if (slash == parent) slash[1] = '\0';
                        else *slash = '\0';
                        fd = open(parent, O_RDONLY | O_DIRECTORY);
                        if (fd < 0) return 0;
                        if (fsync(fd) != 0) { close(fd); return 0; }
                        if (close(fd) != 0) return 0;
                    }
                }
            } else if (current[0] && !S_ISDIR(st.st_mode)) {
                return 0;
            }
            current[i] = saved;
        }
    }
    return 1;
}

static int write_all(int fd, const void* data, size_t length)
{
    const unsigned char* p = (const unsigned char*)data;
    while (length) {
        ssize_t n = write(fd, p, length);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return 0;
        p += (size_t)n;
        length -= (size_t)n;
    }
    return 1;
}

static int read_exact(int fd, void* data, size_t length)
{
    unsigned char* p = (unsigned char*)data;
    while (length) {
        ssize_t n = read(fd, p, length);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return 0;
        p += (size_t)n;
        length -= (size_t)n;
    }
    return 1;
}

/* Returns 1 for a valid identical record, 0 for a valid different record,
 * and -1 for malformed, truncated, unreadable, or checksum-invalid contents. */
static int compare_existing(const char* path, uint64_t session, uint32_t sequence,
                            const void* payload, size_t length)
{
    char header[HEADER_LIMIT];
    size_t used = 0;
    uint64_t stored_session;
    uint32_t stored_sequence, stored_crc;
    size_t stored_length;
    int consumed = 0;
    struct stat st;
    int fd = open(path, O_RDONLY | O_NOFOLLOW);
    int same;
    unsigned char* actual;
    if (fd < 0) return -1;
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode)) { close(fd); return -1; }
    while (used + 1 < sizeof(header)) {
        if (!read_exact(fd, header + used, 1)) { close(fd); return -1; }
        if (header[used++] == '\n') break;
    }
    if (!used || header[used - 1] != '\n') { close(fd); return -1; }
    header[used] = '\0';
    if (sscanf(header, "FQI1,%16" SCNx64 ",%" SCNu32 ",%zu,%8" SCNx32 "\n%n",
               &stored_session, &stored_sequence, &stored_length, &stored_crc, &consumed) != 4 ||
        consumed != (int)used) { close(fd); return -1; }
    if (stored_session != session || stored_sequence != sequence) { close(fd); return -1; }
    if (stored_length > (size_t)SSIZE_MAX) { close(fd); return -1; }
    actual = (unsigned char*)malloc(stored_length ? stored_length : 1);
    if (!actual) { close(fd); return -1; }
    if (!read_exact(fd, actual, stored_length)) { free(actual); close(fd); return -1; }
    {
        unsigned char extra;
        ssize_t n;
        do { n = read(fd, &extra, 1); } while (n < 0 && errno == EINTR);
        if (n != 0) { free(actual); close(fd); return -1; }
    }
    if (crc32_bytes(actual, stored_length) != stored_crc) {
        free(actual);
        close(fd);
        return -1;
    }
    same = stored_length == length && stored_crc == crc32_bytes((const unsigned char*)payload, length) &&
           (length == 0 || memcmp(actual, payload, length) == 0);
    free(actual);
    {
        int sync_ok = fsync(fd) == 0;
        int close_ok = close(fd) == 0;
        if (!sync_ok || !close_ok) return -1;
    }
    return same ? 1 : 0;
}

CaptureInboxResult captureInboxStore(const char* root, const char* device_id,
                                     uint64_t session, uint32_t sequence,
                                     const void* payload, size_t length)
{
    char inbox[PATH_MAX], device[PATH_MAX], final_path[PATH_MAX];
    char temp_path[PATH_MAX], header[HEADER_LIMIT], filename[96];
    int dirfd = -1, fd = -1, result;
    uint32_t checksum;
    struct stat st;

    if (!root || !*root || !valid_device_id(device_id) || (!payload && length) ||
        length > (size_t)SSIZE_MAX || strlen(root) >= PATH_MAX - 160)
        return CAPTURE_INBOX_INVALID;
    if (snprintf(inbox, sizeof(inbox), "%s/%s", root, INBOX_DIR) >= (int)sizeof(inbox) ||
        snprintf(device, sizeof(device), "%s/%s", inbox, device_id) >= (int)sizeof(device))
        return CAPTURE_INBOX_INVALID;
    if (!ensure_directory(root) || !ensure_directory(inbox) || !ensure_directory(device))
        return CAPTURE_INBOX_ERROR;

    checksum = crc32_bytes((const unsigned char*)payload, length);
    if (snprintf(header, sizeof(header), "FQI1,%016" PRIx64 ",%" PRIu32 ",%zu,%08" PRIx32 "\n",
                 session, sequence, length, checksum) >= (int)sizeof(header) ||
        snprintf(filename, sizeof(filename), "%016" PRIx64 "-%" PRIu32 ".fqi", session, sequence) >=
            (int)sizeof(filename) ||
        snprintf(final_path, sizeof(final_path), "%s/%s", device, filename) >= (int)sizeof(final_path))
        return CAPTURE_INBOX_INVALID;

    dirfd = open(device, O_RDONLY | O_DIRECTORY);
    if (dirfd < 0) return CAPTURE_INBOX_ERROR;
    if (fstatat(dirfd, filename, &st, AT_SYMLINK_NOFOLLOW) == 0) {
        result = compare_existing(final_path, session, sequence, payload, length);
        if (result == 1 && fsync(dirfd) != 0) result = -1;
        close(dirfd);
        return result == 1 ? CAPTURE_INBOX_DUPLICATE :
               result == 0 ? CAPTURE_INBOX_CONFLICT : CAPTURE_INBOX_ERROR;
    }
    if (errno != ENOENT) { close(dirfd); return CAPTURE_INBOX_ERROR; }

    if (snprintf(temp_path, sizeof(temp_path), "%s/.capture-%ld-XXXXXX", device, (long)getpid()) >=
        (int)sizeof(temp_path)) { close(dirfd); return CAPTURE_INBOX_INVALID; }
    fd = mkstemp(temp_path);
    if (fd < 0) { close(dirfd); return CAPTURE_INBOX_ERROR; }
    (void)fchmod(fd, 0600);
    if (!write_all(fd, header, strlen(header)) || !write_all(fd, payload, length) ||
        fsync(fd) != 0) {
        close(fd);
        unlink(temp_path);
        close(dirfd);
        return CAPTURE_INBOX_ERROR;
    }
    if (close(fd) != 0) {
        fd = -1;
        unlink(temp_path);
        close(dirfd);
        return CAPTURE_INBOX_ERROR;
    }
    fd = -1;

    /* link() is an atomic no-overwrite publication on the same filesystem. */
    if (link(temp_path, final_path) != 0) {
        int saved_errno = errno;
        unlink(temp_path);
        if (saved_errno == EEXIST) {
            result = compare_existing(final_path, session, sequence, payload, length);
            if (result == 1 && fsync(dirfd) != 0) result = -1;
            close(dirfd);
            return result == 1 ? CAPTURE_INBOX_DUPLICATE :
                   result == 0 ? CAPTURE_INBOX_CONFLICT : CAPTURE_INBOX_ERROR;
        }
        close(dirfd);
        return CAPTURE_INBOX_ERROR;
    }
    if (fsync(dirfd) != 0) {
        /* Do not claim acceptance; leave the complete record for a safe retry. */
        unlink(temp_path);
        close(dirfd);
        return CAPTURE_INBOX_ERROR;
    }
    if (unlink(temp_path) != 0 || fsync(dirfd) != 0) {
        close(dirfd);
        return CAPTURE_INBOX_ERROR;
    }
    close(dirfd);
    return CAPTURE_INBOX_STORED;
}

#endif
