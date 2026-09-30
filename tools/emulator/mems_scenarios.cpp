#include "mems_scenario.h"

void report(const char* name, bool passed, bool fault, double observed);
static int failedRegister = -1;

bool ICM_42627::readBytes(uint8_t address, uint8_t count, uint8_t* destination)
{
    // Fill even failed transfers to make the old ignored-error path deterministic.
    memset(destination, 0, count);
    if (address == failedRegister) return false;
    if (address != TEMP_OUT_H_REG) destination[0] = 0x10;
    return true;
}

void runMEMSScenarios()
{
    ICM_42627 sensor;
    for (int address : {ACCEL_XOUT_H_REG, GYRO_XOUT_H_REG, TEMP_OUT_H_REG}) {
        failedRegister = address;
        float acc[3] = {7, 7, 7};
        float gyro[3] = {8, 8, 8};
        float temperature = 9;
        const bool okay = sensor.read(acc, gyro, nullptr, &temperature);
        const bool unchanged = acc[0] == 7 && acc[1] == 7 && acc[2] == 7 &&
                               gyro[0] == 8 && gyro[1] == 8 && gyro[2] == 8 && temperature == 9;
        const char* name = address == ACCEL_XOUT_H_REG ? "IMU rejects failed acceleration read" :
                           address == GYRO_XOUT_H_REG ? "IMU rejects failed gyro read" : "IMU rejects failed temperature read";
        report(name, !okay && unchanged, true, okay);
    }
    failedRegister = -1;
    float acc[3] = {};
    float gyro[3] = {};
    float temperature = 0;
    const bool okay = sensor.read(acc, gyro, nullptr, &temperature);
    report("successful IMU conversion", okay && acc[0] == 4096 * aRes && gyro[0] == 4096 * gRes &&
           temperature == 25, false, temperature);
}
