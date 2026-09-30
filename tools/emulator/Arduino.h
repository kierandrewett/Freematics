#pragma once
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <cctype>
#include <algorithm>

using std::min;
using std::max;
struct QuietSerial {
    template <typename T> void print(const T&) {}
    template <typename T> void println(const T&) {}
};
inline QuietSerial Serial;

using byte = uint8_t;
extern uint32_t simulationTime;
inline unsigned long millis() { return simulationTime; }
inline void delay(unsigned long duration) { simulationTime += duration; }
