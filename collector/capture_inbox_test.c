#define _GNU_SOURCE
#include "capture_inbox.h"
#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int read_capture(const char* path, char* output, size_t capacity)
{
    DIR* directory = opendir(path);
    if (!directory) return 0;
    struct dirent* entry;
    char file[1024];
    int found = 0;
    while ((entry = readdir(directory)) != NULL) {
        size_t length = strlen(entry->d_name);
        if (length < 5 || strcmp(entry->d_name + length - 4, ".fqi")) continue;
        if (snprintf(file, sizeof(file), "%s/%s", path, entry->d_name) >= (int)sizeof(file)) break;
        FILE* stream = fopen(file, "rb");
        if (!stream) break;
        size_t bytes = fread(output, 1, capacity, stream);
        found = !ferror(stream) && !fclose(stream) && bytes < capacity;
        if (found) output[bytes] = 0;
        break;
    }
    closedir(directory);
    return found;
}

int main(void)
{
    char root[] = "/tmp/freematics-inbox-XXXXXX";
    if (!mkdtemp(root)) return 1;
    const char frame[] = "0:1234,10C:781,24:1420,";
    const uint64_t session = UINT64_C(0x123456789abcdef0);
    const uint32_t sequence = 0x10203040U;
    const CaptureInboxResult first = captureInboxStore(root, "DEVICE42", session, sequence,
                                                        frame, sizeof(frame) - 1);
    char deviceDir[1024], stored[2048];
    snprintf(deviceDir, sizeof(deviceDir), "%s/capture-inbox/DEVICE42", root);
    const int persisted = read_capture(deviceDir, stored, sizeof(stored));
    const int content_ok = persisted && strstr(stored, "FQI1,123456789abcdef0,270544960,") == stored &&
        strstr(stored, frame) != NULL;
    const CaptureInboxResult retry = captureInboxStore(root, "DEVICE42", session, sequence,
                                                        frame, sizeof(frame) - 1);
    const char changed[] = "0:1234,10C:999,";
    const CaptureInboxResult collision = captureInboxStore(root, "DEVICE42", session, sequence,
                                                            changed, sizeof(changed) - 1);
    const CaptureInboxResult unsafe = captureInboxStore(root, "../DEVICE42", session, sequence,
                                                         frame, sizeof(frame) - 1);
    CaptureInboxReceipt receipt_first, receipt_retry;
    const CaptureInboxResult receipt_new = captureInboxReceiptEnsure(root, "DEVICE42", session,
        sequence, INT64_C(1800000000), &receipt_first);
    const CaptureInboxResult receipt_duplicate = captureInboxReceiptEnsure(root, "DEVICE42", session,
        sequence, INT64_C(1900000000), &receipt_retry);
    char receipt_path[1200], receipt_record[160];
    snprintf(receipt_path, sizeof(receipt_path), "%s/%016llx-%u.receipt", deviceDir,
        (unsigned long long)session, sequence);
    FILE* receipt_file = fopen(receipt_path, "rb");
    size_t receipt_length = receipt_file ? fread(receipt_record, 1, sizeof(receipt_record) - 1, receipt_file) : 0;
    if (receipt_file) fclose(receipt_file);
    receipt_record[receipt_length] = 0;
    const int receipt_exact = strcmp(receipt_record,
        "FQR1,123456789abcdef0,270544960,1800000000000,1,449f3036\n") == 0;
    const CaptureInboxResult bad_identity = captureInboxReceiptEnsure(root, "../DEVICE42", session,
        sequence, INT64_C(1800000000), NULL);
    int corrupt_fd = -1;
    const uint64_t other_session = UINT64_C(0x223456789abcdef0);
    const int other_capture_ok = captureInboxStore(root, "DEVICE42", other_session, sequence,
        frame, sizeof(frame) - 1) == CAPTURE_INBOX_STORED;
    const int other_receipt_ok = captureInboxReceiptEnsure(root, "DEVICE42", other_session,
        sequence, INT64_C(1800000000), NULL) == CAPTURE_INBOX_STORED;
    char other_receipt_path[1200], other_record[160];
    snprintf(other_receipt_path, sizeof(other_receipt_path), "%s/%016llx-%u.receipt", deviceDir,
        (unsigned long long)other_session, sequence);
    FILE* other_file = fopen(other_receipt_path, "rb");
    size_t other_length = other_file ? fread(other_record, 1, sizeof(other_record), other_file) : 0;
    if (other_file) fclose(other_file);
    int mismatch_written = 0;
    if (other_capture_ok && other_receipt_ok && other_length < sizeof(other_record)) {
        corrupt_fd = open(receipt_path, O_WRONLY | O_TRUNC);
        if (corrupt_fd >= 0) {
            mismatch_written = write(corrupt_fd, other_record, other_length) == (ssize_t)other_length;
            close(corrupt_fd);
        }
    }
    const CaptureInboxResult mismatched_identity = captureInboxReceiptEnsure(root, "DEVICE42", session,
        sequence, INT64_C(1800000000), NULL);
    corrupt_fd = open(receipt_path, O_WRONLY | O_TRUNC);
    if (corrupt_fd >= 0) { (void)write(corrupt_fd, "corrupt\n", 8); close(corrupt_fd); }
    const CaptureInboxResult corrupt = captureInboxReceiptEnsure(root, "DEVICE42", session,
        sequence, INT64_C(1800000000), NULL);
    const int receipt_ok = receipt_new == CAPTURE_INBOX_STORED &&
        receipt_duplicate == CAPTURE_INBOX_DUPLICATE && receipt_first.epoch_ms == UINT64_C(1800000000000) &&
        receipt_retry.epoch_ms == receipt_first.epoch_ms && receipt_retry.clock_plausible == 1 &&
        receipt_exact && bad_identity == CAPTURE_INBOX_INVALID && mismatch_written &&
        mismatched_identity == CAPTURE_INBOX_ERROR && corrupt == CAPTURE_INBOX_ERROR;
    printf("%s: new=%d duplicate=%d conflict=%d unsafe-id=%d exact-payload=%d receipt-new=%d receipt-retry=%d receipt-exact=%d bad-id=%d mismatch=%d corrupt=%d\n",
        first == CAPTURE_INBOX_STORED && retry == CAPTURE_INBOX_DUPLICATE &&
        collision == CAPTURE_INBOX_CONFLICT && unsafe == CAPTURE_INBOX_INVALID && content_ok && receipt_ok
            ? "PASS" : "FAIL", first, retry, collision, unsafe, content_ok, receipt_new,
        receipt_duplicate, receipt_exact, bad_identity, mismatched_identity, corrupt);
    return first == CAPTURE_INBOX_STORED && retry == CAPTURE_INBOX_DUPLICATE &&
        collision == CAPTURE_INBOX_CONFLICT && unsafe == CAPTURE_INBOX_INVALID && content_ok && receipt_ok ? 0 : 1;
}
