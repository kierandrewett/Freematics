#pragma once
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <cctype>

using byte = uint8_t;
extern uint32_t simulationTime;
inline unsigned long millis() { return simulationTime; }
inline void delay(unsigned long duration) { simulationTime += duration; }
