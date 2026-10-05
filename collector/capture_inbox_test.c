#define _GNU_SOURCE
#include "capture_inbox.h"
#include <dirent.h>
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
    printf("%s: new=%d duplicate=%d conflict=%d unsafe-id=%d exact-payload=%d\n",
        first == CAPTURE_INBOX_STORED && retry == CAPTURE_INBOX_DUPLICATE &&
        collision == CAPTURE_INBOX_CONFLICT && unsafe == CAPTURE_INBOX_INVALID && content_ok
            ? "PASS" : "FAIL", first, retry, collision, unsafe, content_ok);
    return first == CAPTURE_INBOX_STORED && retry == CAPTURE_INBOX_DUPLICATE &&
        collision == CAPTURE_INBOX_CONFLICT && unsafe == CAPTURE_INBOX_INVALID && content_ok ? 0 : 1;
}
