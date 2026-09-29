#ifndef SDACCESS_H_INCLUDED
#define SDACCESS_H_INCLUDED

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

bool lockSD();
void unlockSD();

class SDGuard {
public:
    SDGuard() : locked(lockSD()) {}
    ~SDGuard() { if (locked) unlockSD(); }
    explicit operator bool() const { return locked; }
private:
    bool locked;
    SDGuard(const SDGuard&) = delete;
    SDGuard& operator=(const SDGuard&) = delete;
};

#endif
