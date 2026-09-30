#pragma once
#include <algorithm>
#include <cstdarg>
#include <map>
#include <memory>
#include <string>
#include <vector>
#include "Arduino.h"

constexpr int FILE_READ = 0;
constexpr int FILE_WRITE = 1;
constexpr int FILE_APPEND = 2;

// Only these bytes survive a simulated device restart. No physical SD driver runs.
inline std::map<std::string, std::shared_ptr<std::vector<uint8_t>>> cardFiles;
inline bool cardOnline = true;
inline bool cardRenameFails = false;
inline int64_t cardWriteBudget = -1;

class File
{
public:
    File() = default;
    File(std::shared_ptr<std::vector<uint8_t>> bytes, bool writable, size_t position)
        : bytes(bytes), writable(writable), position(position) {}
    explicit operator bool() const { return cardOnline && bytes != nullptr; }
    uint32_t size() const { return *this ? bytes->size() : 0; }
    bool seek(uint32_t offset) { if (!*this || offset > bytes->size()) return false; position = offset; return true; }
    size_t read(uint8_t* target, size_t count)
    {
        if (!*this) return 0;
        count = std::min(count, bytes->size() - position);
        if (!count) return 0;
        memcpy(target, bytes->data() + position, count);
        position += count;
        return count;
    }
    size_t write(const uint8_t* source, size_t count)
    {
        if (!*this || !writable) return 0;
        if (cardWriteBudget >= 0) {
            count = std::min(count, static_cast<size_t>(cardWriteBudget));
            cardWriteBudget -= count;
        }
        if (!count) return 0;
        bytes->resize(std::max(bytes->size(), position + count));
        memcpy(bytes->data() + position, source, count);
        position += count;
        return count;
    }
    int printf(const char* format, ...)
    {
        char text[128];
        va_list args;
        va_start(args, format);
        const int count = vsnprintf(text, sizeof(text), format, args);
        va_end(args);
        return write(reinterpret_cast<const uint8_t*>(text), std::min(count, 127));
    }
    void flush() {}
    void close() { bytes.reset(); }
private:
    std::shared_ptr<std::vector<uint8_t>> bytes;
    bool writable = false;
    size_t position = 0;
};

class DummySD
{
public:
    bool exists(const char* path) { return cardOnline && cardFiles.count(path); }
    bool mkdir(const char*) { return cardOnline; }
    bool remove(const char* path) { return cardOnline && cardFiles.erase(path); }
    bool rename(const char* from, const char* to)
    {
        if (!cardOnline || cardRenameFails || !exists(from) || exists(to)) return false;
        cardFiles[to] = cardFiles.at(from);
        cardFiles.erase(from);
        return true;
    }
    File open(const char* path, int mode)
    {
        if (!cardOnline || (mode == FILE_READ && !exists(path))) return {};
        if (!exists(path)) cardFiles[path] = std::make_shared<std::vector<uint8_t>>();
        auto bytes = cardFiles.at(path);
        if (mode == FILE_WRITE) bytes->clear();
        return {bytes, mode != FILE_READ, mode == FILE_APPEND ? bytes->size() : 0};
    }
};
inline DummySD SD;
