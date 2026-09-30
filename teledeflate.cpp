// Minimal zlib/deflate encoder for upload batches.
//
// The ESP32 ROM compressor failed on this board (revision 1 chip, no PSRAM
// workaround in ROM code), so the firmware compiles its own. One deflate block
// with the fixed Huffman code (RFC 1951 3.2.6) keeps the encoder small and
// easy to check; telemetry text is repetitive, so LZ77 matching does most of
// the work. tools/check-deflate.py verifies it against zlib on the host.

#include "teledeflate.h"
#include <string.h>

namespace {

const uint16_t kLengthBase[29] = {3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31,
                                  35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258};
const uint8_t kLengthExtra[29] = {0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2,
                                  3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0};
const uint16_t kDistanceBase[30] = {1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193,
                                    257, 385, 513, 769, 1025, 1537, 2049, 3073, 4097, 6145,
                                    8193, 12289, 16385, 24577};
const uint8_t kDistanceExtra[30] = {0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6,
                                    7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13};
const unsigned kMinMatch = 3;
const unsigned kMaxMatch = 258;
// Chain steps per position. More steps find longer matches but cost time.
const unsigned kMaxChain = 16;

struct BitWriter {
    uint8_t* out;
    size_t capacity;
    size_t length;
    uint32_t bits;
    unsigned count;
    bool overflow;

    void put(uint32_t value, unsigned width)
    {
        bits |= value << count;
        count += width;
        while (count >= 8) {
            if (length >= capacity) { overflow = true; return; }
            out[length++] = (uint8_t)bits;
            bits >>= 8;
            count -= 8;
        }
    }
    // Huffman codes are defined most significant bit first.
    void code(uint32_t value, unsigned width)
    {
        uint32_t reversed = 0;
        for (unsigned i = 0; i < width; i++) reversed |= ((value >> i) & 1) << (width - 1 - i);
        put(reversed, width);
    }
    void flush()
    {
        if (count) put(0, 8 - count);
    }
};

void literal(BitWriter& writer, unsigned symbol)
{
    if (symbol < 144) writer.code(0x30 + symbol, 8);
    else if (symbol < 256) writer.code(0x190 + symbol - 144, 9);
    else if (symbol < 280) writer.code(symbol - 256, 7);
    else writer.code(0xC0 + symbol - 280, 8);
}

void match(BitWriter& writer, unsigned length, unsigned distance)
{
    unsigned index = 28;
    while (kLengthBase[index] > length) index--;
    literal(writer, 257 + index);
    writer.put(length - kLengthBase[index], kLengthExtra[index]);
    index = 29;
    while (kDistanceBase[index] > distance) index--;
    writer.code(index, 5);
    writer.put(distance - kDistanceBase[index], kDistanceExtra[index]);
}

inline unsigned hash3(const uint8_t* p)
{
    return ((((uint32_t)p[0] << 16) | ((uint32_t)p[1] << 8) | p[2]) * 2654435761u) >> 20;
}

}

size_t zlibCompress(const uint8_t* input, size_t length, uint8_t* output, size_t capacity,
                    DeflateScratch* scratch)
{
    if ((!input && length) || !output || !scratch || capacity < 6) return 0;
    memset(scratch->head, 0, sizeof(scratch->head));
    BitWriter writer = {output, capacity, 0, 0, 0, false};
    // zlib header: deflate, 32 KB window, no preset dictionary, check bits.
    writer.put(0x78, 8);
    writer.put(0x01, 8);
    // One final block using the fixed Huffman code.
    writer.put(1, 1);
    writer.put(1, 2);

    size_t position = 0;
    while (position < length && !writer.overflow) {
        unsigned bestLength = 0;
        unsigned bestDistance = 0;
        if (length - position >= kMinMatch) {
            const unsigned h = hash3(input + position);
            uint32_t candidate = scratch->head[h];
            const unsigned limit = length - position < kMaxMatch ? (unsigned)(length - position) : kMaxMatch;
            for (unsigned step = 0; candidate && step < kMaxChain; step++) {
                const size_t from = candidate - 1;
                const size_t distance = position - from;
                if (distance > DEFLATE_WINDOW) break;
                unsigned run = 0;
                while (run < limit && input[from + run] == input[position + run]) run++;
                if (run > bestLength) {
                    bestLength = run;
                    bestDistance = (unsigned)distance;
                    if (run == limit) break;
                }
                const uint16_t back = scratch->chain[from & (DEFLATE_WINDOW - 1)];
                candidate = back && back <= from ? (uint32_t)(from - back + 1) : 0;
            }
        }
        const size_t advance = bestLength >= kMinMatch ? bestLength : 1;
        if (bestLength >= kMinMatch) match(writer, bestLength, bestDistance);
        else literal(writer, input[position]);
        // Index every covered position so later text can refer back to it.
        for (size_t end = position + advance; position < end; position++) {
            if (length - position < kMinMatch) continue;
            const unsigned h = hash3(input + position);
            const uint32_t previous = scratch->head[h];
            const size_t delta = previous ? position - (previous - 1) : 0;
            scratch->chain[position & (DEFLATE_WINDOW - 1)] = delta && delta < DEFLATE_WINDOW ? (uint16_t)delta : 0;
            scratch->head[h] = (uint32_t)position + 1;
        }
    }
    literal(writer, 256);
    writer.flush();

    uint32_t a = 1, b = 0;
    for (size_t i = 0; i < length; i++) {
        a = (a + input[i]) % 65521;
        b = (b + a) % 65521;
    }
    const uint32_t adler = (b << 16) | a;
    for (int shift = 24; shift >= 0; shift -= 8) writer.put((adler >> shift) & 0xFF, 8);
    return writer.overflow ? 0 : writer.length;
}
