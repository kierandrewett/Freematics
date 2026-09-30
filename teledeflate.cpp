// Minimal zlib/deflate encoder for upload batches.
//
// The ESP32 ROM compressor failed on this board (revision 1 chip, no PSRAM
// workaround in ROM code), so the firmware compiles its own. It writes one
// deflate block with dynamic Huffman codes (RFC 1951 3.2.7). Telemetry text
// is mostly digits, commas and colons, so codes built for each batch beat the
// fixed code by a wide margin. LZ77 runs twice over the input: the first pass
// counts symbols, the second writes them, so no token buffer is needed.
// tools/check-deflate.py verifies the output against zlib on the host.

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
// Order in which code-length code lengths are stored (RFC 1951 3.2.7).
const uint8_t kCodeLengthOrder[19] = {16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15};
const unsigned kMinMatch = 3;
const unsigned kMaxMatch = 258;
// Chain steps per position. More steps find longer matches but cost time.
const unsigned kMaxChain = 16;
const unsigned kLiteralCodes = 286;
const unsigned kDistanceCodes = 30;

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
            if (length >= capacity) { overflow = true; count = 0; bits = 0; return; }
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

inline unsigned lengthIndex(unsigned length)
{
    unsigned index = 28;
    while (kLengthBase[index] > length) index--;
    return index;
}

inline unsigned distanceIndex(unsigned distance)
{
    unsigned index = 29;
    while (kDistanceBase[index] > distance) index--;
    return index;
}

inline unsigned hash3(const uint8_t* p)
{
    return ((((uint32_t)p[0] << 16) | ((uint32_t)p[1] << 8) | p[2]) * 2654435761u) >> 20;
}

// Greedy LZ77 with hash chains. Calls sink.literal() or sink.match().
template <typename Sink>
void parse(const uint8_t* input, size_t length, DeflateScratch* scratch, Sink& sink)
{
    memset(scratch->head, 0, sizeof(scratch->head));
    size_t position = 0;
    while (position < length && !sink.stopped()) {
        unsigned bestLength = 0;
        unsigned bestDistance = 0;
        if (length - position >= kMinMatch) {
            uint32_t candidate = scratch->head[hash3(input + position)];
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
        if (bestLength >= kMinMatch) sink.match(bestLength, bestDistance);
        else sink.literal(input[position]);
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
}

struct Counter {
    uint32_t literals[kLiteralCodes];
    uint32_t distances[kDistanceCodes];
    void literal(unsigned byte) { literals[byte]++; }
    void match(unsigned length, unsigned distance)
    {
        literals[257 + lengthIndex(length)]++;
        distances[distanceIndex(distance)]++;
    }
    bool stopped() const { return false; }
};

// Huffman code lengths for `count` symbols, limited to maxBits. Built from a
// plain Huffman tree, then over-long codes are folded back with the Kraft
// adjustment miniz uses; the most frequent symbols get the shortest codes.
void buildLengths(const uint32_t* frequency, unsigned count, unsigned maxBits, uint8_t* lengths)
{
    uint32_t weight[2 * kLiteralCodes];
    int16_t parent[2 * kLiteralCodes];
    bool open[2 * kLiteralCodes];
    uint16_t used[kLiteralCodes];
    unsigned symbols = 0;
    memset(lengths, 0, count);
    for (unsigned i = 0; i < count; i++) {
        if (frequency[i]) used[symbols++] = (uint16_t)i;
    }
    if (symbols == 0) return;
    if (symbols == 1) { lengths[used[0]] = 1; return; }
    unsigned nodes = symbols;
    for (unsigned i = 0; i < symbols; i++) {
        weight[i] = frequency[used[i]];
        parent[i] = -1;
        open[i] = true;
    }
    for (unsigned merged = 0; merged + 1 < symbols; merged++) {
        int first = -1, second = -1;
        for (unsigned i = 0; i < nodes; i++) {
            if (!open[i]) continue;
            if (first < 0 || weight[i] < weight[first]) { second = first; first = (int)i; }
            else if (second < 0 || weight[i] < weight[second]) second = (int)i;
        }
        open[first] = open[second] = false;
        weight[nodes] = weight[first] + weight[second];
        parent[nodes] = -1;
        open[nodes] = true;
        parent[first] = parent[second] = (int16_t)nodes;
        nodes++;
    }
    unsigned numCodes[33] = {0};
    for (unsigned i = 0; i < symbols; i++) {
        unsigned depth = 0;
        for (int node = (int)i; parent[node] >= 0; node = parent[node]) depth++;
        numCodes[depth > 32 ? 32 : depth]++;
    }
    for (unsigned i = maxBits + 1; i <= 32; i++) {
        numCodes[maxBits] += numCodes[i];
        numCodes[i] = 0;
    }
    uint32_t total = 0;
    for (unsigned i = maxBits; i > 0; i--) total += (uint32_t)numCodes[i] << (maxBits - i);
    while (total != (1u << maxBits)) {
        numCodes[maxBits]--;
        for (unsigned i = maxBits - 1; i > 0; i--) {
            if (numCodes[i]) {
                numCodes[i]--;
                numCodes[i + 1] += 2;
                break;
            }
        }
        total--;
    }
    // Most frequent first (stable on symbol number), then hand out lengths.
    for (unsigned i = 1; i < symbols; i++) {
        const uint16_t symbol = used[i];
        unsigned j = i;
        while (j > 0 && frequency[used[j - 1]] < frequency[symbol]) {
            used[j] = used[j - 1];
            j--;
        }
        used[j] = symbol;
    }
    unsigned next = 0;
    for (unsigned bits = 1; bits <= maxBits; bits++) {
        for (unsigned n = 0; n < numCodes[bits]; n++) lengths[used[next++]] = (uint8_t)bits;
    }
}

// Canonical codes from code lengths (RFC 1951 3.2.2).
void buildCodes(const uint8_t* lengths, unsigned count, uint16_t* codes)
{
    unsigned lengthCount[16] = {0};
    for (unsigned i = 0; i < count; i++) lengthCount[lengths[i]]++;
    lengthCount[0] = 0;
    unsigned nextCode[16] = {0};
    unsigned code = 0;
    for (unsigned bits = 1; bits < 16; bits++) {
        code = (code + lengthCount[bits - 1]) << 1;
        nextCode[bits] = code;
    }
    for (unsigned i = 0; i < count; i++) {
        codes[i] = lengths[i] ? (uint16_t)nextCode[lengths[i]]++ : 0;
    }
}

struct Writer {
    BitWriter* bits;
    const uint8_t* literalLengths;
    const uint16_t* literalCodes;
    const uint8_t* distanceLengths;
    const uint16_t* distanceCodes;
    void literal(unsigned byte) { bits->code(literalCodes[byte], literalLengths[byte]); }
    void match(unsigned length, unsigned distance)
    {
        const unsigned lengthSymbol = lengthIndex(length);
        bits->code(literalCodes[257 + lengthSymbol], literalLengths[257 + lengthSymbol]);
        bits->put(length - kLengthBase[lengthSymbol], kLengthExtra[lengthSymbol]);
        const unsigned distanceSymbol = distanceIndex(distance);
        bits->code(distanceCodes[distanceSymbol], distanceLengths[distanceSymbol]);
        bits->put(distance - kDistanceBase[distanceSymbol], kDistanceExtra[distanceSymbol]);
    }
    bool stopped() const { return bits->overflow; }
};

// Run-length code the literal and distance code lengths with symbols 0-18.
// With `bits` null it only counts symbol use.
void codeLengths(const uint8_t* lengths, unsigned count, uint32_t* frequency, BitWriter* bits,
                 const uint8_t* clLengths, const uint16_t* clCodes)
{
    auto emit = [&](unsigned symbol, unsigned extra, unsigned width) {
        if (!bits) { frequency[symbol]++; return; }
        bits->code(clCodes[symbol], clLengths[symbol]);
        if (width) bits->put(extra, width);
    };
    unsigned i = 0;
    while (i < count) {
        const uint8_t value = lengths[i];
        unsigned run = 1;
        while (i + run < count && lengths[i + run] == value) run++;
        i += run;
        if (value == 0) {
            while (run >= 11) {
                const unsigned part = run < 138 ? run : 138;
                emit(18, part - 11, 7);
                run -= part;
            }
            if (run >= 3) {
                emit(17, run - 3, 3);
                run = 0;
            }
            while (run--) emit(0, 0, 0);
        } else {
            emit(value, 0, 0);
            run--;
            while (run >= 3) {
                const unsigned part = run < 6 ? run : 6;
                emit(16, part - 3, 2);
                run -= part;
            }
            while (run--) emit(value, 0, 0);
        }
    }
}

}

size_t zlibCompress(const uint8_t* input, size_t length, uint8_t* output, size_t capacity,
                    DeflateScratch* scratch)
{
    if ((!input && length) || !output || !scratch || capacity < 6) return 0;

    Counter counter;
    memset(&counter, 0, sizeof(counter));
    parse(input, length, scratch, counter);
    counter.literals[256] = 1; // end of block
    uint8_t literalLengths[kLiteralCodes];
    uint8_t distanceLengths[kDistanceCodes];
    buildLengths(counter.literals, kLiteralCodes, 15, literalLengths);
    buildLengths(counter.distances, kDistanceCodes, 15, distanceLengths);
    unsigned literalCount = kLiteralCodes;
    while (literalCount > 257 && !literalLengths[literalCount - 1]) literalCount--;
    unsigned distanceCount = kDistanceCodes;
    while (distanceCount > 1 && !distanceLengths[distanceCount - 1]) distanceCount--;
    // A block without matches still declares one distance code.
    if (!distanceLengths[0] && distanceCount == 1) distanceLengths[0] = 1;
    uint16_t literalCodes[kLiteralCodes];
    uint16_t distanceCodes[kDistanceCodes];
    buildCodes(literalLengths, kLiteralCodes, literalCodes);
    buildCodes(distanceLengths, kDistanceCodes, distanceCodes);

    // Literal and distance code lengths are sent as one run-length sequence.
    uint8_t combined[kLiteralCodes + kDistanceCodes];
    memcpy(combined, literalLengths, literalCount);
    memcpy(combined + literalCount, distanceLengths, distanceCount);
    const unsigned combinedCount = literalCount + distanceCount;
    uint32_t clFrequency[19] = {0};
    codeLengths(combined, combinedCount, clFrequency, nullptr, nullptr, nullptr);
    uint8_t clLengths[19];
    uint16_t clCodes[19];
    buildLengths(clFrequency, 19, 7, clLengths);
    buildCodes(clLengths, 19, clCodes);
    unsigned clCount = 19;
    while (clCount > 4 && !clLengths[kCodeLengthOrder[clCount - 1]]) clCount--;

    BitWriter bits = {output, capacity, 0, 0, 0, false};
    // zlib header: deflate, 32 KB window, no preset dictionary, check bits.
    bits.put(0x78, 8);
    bits.put(0x01, 8);
    // One final block with dynamic Huffman codes.
    bits.put(1, 1);
    bits.put(2, 2);
    bits.put(literalCount - 257, 5);
    bits.put(distanceCount - 1, 5);
    bits.put(clCount - 4, 4);
    for (unsigned i = 0; i < clCount; i++) bits.put(clLengths[kCodeLengthOrder[i]], 3);
    codeLengths(combined, combinedCount, nullptr, &bits, clLengths, clCodes);

    Writer writer = {&bits, literalLengths, literalCodes, distanceLengths, distanceCodes};
    parse(input, length, scratch, writer);
    bits.code(literalCodes[256], literalLengths[256]);
    bits.flush();

    uint32_t a = 1, b = 0;
    for (size_t i = 0; i < length; i++) {
        a = (a + input[i]) % 65521;
        b = (b + a) % 65521;
    }
    const uint32_t adler = (b << 16) | a;
    for (int shift = 24; shift >= 0; shift -= 8) bits.put((adler >> shift) & 0xFF, 8);
    return bits.overflow ? 0 : bits.length;
}
