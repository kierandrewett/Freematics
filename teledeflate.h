#ifndef TELEDEFLATE_H_INCLUDED
#define TELEDEFLATE_H_INCLUDED

#include <stddef.h>
#include <stdint.h>

// Hash table entries used by zlibCompress(). The caller owns the table so the
// encoder allocates nothing: 4096 heads plus a 32 KB chain window.
#define DEFLATE_HASH_SIZE 4096
#define DEFLATE_WINDOW 32768

struct DeflateScratch {
    uint32_t head[DEFLATE_HASH_SIZE];
    uint16_t chain[DEFLATE_WINDOW];
};

// Compress `length` bytes into a zlib stream (RFC 1950 wrapper, one RFC 1951
// block with fixed Huffman codes). Returns the stream length, or 0 when the
// output does not fit in `capacity`. Any zlib inflate can read the result.
size_t zlibCompress(const uint8_t* input, size_t length, uint8_t* output, size_t capacity,
                    DeflateScratch* scratch);

#endif
