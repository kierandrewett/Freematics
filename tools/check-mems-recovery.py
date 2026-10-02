#!/usr/bin/env python3
"""Run the production MEMS worker through a persistent I2C fault and recovery."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'telelogger.ino').read_text()
start = source.index('void acquireMEMS(void*)\n{')
opening = source.index('{',start)
end,depth=opening+1,1
while depth:
    depth += (source[end]=='{')-(source[end]=='}')
    end+=1
code=r'''
#include <cstdint>
#include <cassert>
#include <cstring>
#include <iostream>
using byte=uint8_t;
#define ENABLE_OBD 1
#define ENABLE_MEMS 1
#define ENABLE_ORIENTATION 0
#define STATE_WORKING 1
#define STATE_MEMS_READY 2
#define PHASE_TRIP 1
#define PHASE_WRAP_UP 2
#define BUFFER_LENGTH 3072
#define ELEMENT_UINT8 0
#define ELEMENT_UINT32 2
#define ELEMENT_FLOAT 4
#define PID_WAVEFORM_VOLTAGE 0xA0
#define PID_WAVEFORM_MOTION_TIMESTAMP 0xA1
#define PID_WAVEFORM_RAW_ACCELERATION 0xA2
#define PID_WAVEFORM_GYRO 0xA3
#define PID_WAVEFORM_LOSSES 0xA4
#define PID_WAVEFORM_FORMAT 0xA5
#define portMAX_DELAY 0
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
struct ELEMENT_HEAD { uint16_t pid; uint8_t type; uint8_t count; };
struct CBuffer {
    uint16_t offset=0;
    unsigned voltageRecords=0,motionTimestamps=0,rawVectors=0,gyroVectors=0;
    uint32_t firstVoltageTimestamp=0,firstMotionTimestamp=0;
    float firstRawAcceleration=0;
    bool add(uint16_t pid,uint8_t,void* values,int bytes,uint8_t=1) {
        offset += sizeof(ELEMENT_HEAD)+bytes;
        if(pid==PID_WAVEFORM_VOLTAGE) {
            voltageRecords++;
            if(voltageRecords==1) firstVoltageTimestamp=static_cast<uint32_t*>(values)[0];
        }
        if(pid==PID_WAVEFORM_MOTION_TIMESTAMP) {
            motionTimestamps++;
            if(motionTimestamps==1) firstMotionTimestamp=*static_cast<uint32_t*>(values);
        }
        if(pid==PID_WAVEFORM_RAW_ACCELERATION) {
            rawVectors++;
            if(rawVectors==1) firstRawAcceleration=static_cast<float*>(values)[0];
        }
        if(pid==PID_WAVEFORM_GYRO) gyroVectors++;
        return offset<=BUFFER_LENGTH;
    }
};
#include "sensorwaveform.h"
struct Stop {};
uint32_t tick=1000;
uint32_t stopAt=9000;
uint32_t millis() {return tick;}
void delay(unsigned duration) {tick+=duration;if(tick>=stopAt)throw Stop{};}
void xSemaphoreTake(int,int) {}
void xSemaphoreGive(int) {}
void vTaskDelete(void*) {}
int memsMutex,sensorMux;
volatile uint8_t powerPhase=PHASE_TRIP;
float accBias[3]={.25f};
struct {unsigned flags=3;bool check(unsigned mask){return (flags&mask)==mask;}
void set(unsigned mask){flags|=mask;}void clear(unsigned mask){flags&=~mask;}} state;
struct {template<class T>void println(T){} } Serial;
struct { uint8_t devType=13; } sys;
float readVehicleVoltage() { return 14.2f; }
struct MEMSSnapshot {float acceleration[3]{},gyro[3]{},compass[3]{},temperature=0;uint32_t timestamp=0;};
MEMSSnapshot memsSnapshot;
struct IntervalExtremes {} intervalExtremes;
void noteAcceleration(IntervalExtremes&, const float*) {}
void noteVoltage(IntervalExtremes&, float) {}
SensorWaveforms sensorWaveforms;
struct Sensor {
    unsigned initialisations=0,reads=0,successes=0;
    bool read(float* acc,float*,float*,float*,void*) {reads++;acc[0]=.5;const bool success=initialisations>=2;successes+=success;return success;}
    void end() {}
    byte begin(bool=false) {return ++initialisations>=2;}
} sensor,*mems=&sensor;
'''
code+=source[start:end]
code+=r'''
int main(){
    memsSnapshot.timestamp=500;
    try{acquireMEMS(nullptr);}catch(const Stop&){}
    assert(sensor.initialisations>=2);
    assert(state.check(STATE_MEMS_READY));
    assert(tick-memsSnapshot.timestamp<=50);
    assert(memsSnapshot.acceleration[0]==.25f);
    unsigned voltageRecords=0,motionTimestamps=0,rawVectors=0,gyroVectors=0;
    uint32_t firstVoltageTimestamp=0,firstMotionTimestamp=0;
    float firstRawAcceleration=0;
    for(unsigned i=0;i<16;i++) {
        CBuffer captured;
        sensorWaveforms.emit(&captured);
        if(!firstVoltageTimestamp) firstVoltageTimestamp=captured.firstVoltageTimestamp;
        if(!firstMotionTimestamp) firstMotionTimestamp=captured.firstMotionTimestamp;
        if(!firstRawAcceleration) firstRawAcceleration=captured.firstRawAcceleration;
        voltageRecords+=captured.voltageRecords;
        motionTimestamps+=captured.motionTimestamps;
        rawVectors+=captured.rawVectors;
        gyroVectors+=captured.gyroVectors;
    }
    assert(sensor.reads>sensor.successes);
    assert(firstVoltageTimestamp < memsSnapshot.timestamp);
    assert(voltageRecords>0);
    assert(firstMotionTimestamp > firstVoltageTimestamp);
    assert(motionTimestamps>0 && motionTimestamps<=sensor.successes);
    assert(rawVectors==motionTimestamps && gyroVectors==motionTimestamps);
    assert(firstRawAcceleration==.5f);
    assert(!sensorWaveforms.hasPending());

    // Wrap-up preserves fresh watcher snapshots but must not add waveform
    // readings which would otherwise be attached to a later trip on resume.
    const uint32_t watcherBefore=memsSnapshot.timestamp;
    powerPhase=PHASE_WRAP_UP;
    stopAt=11000;
    try{acquireMEMS(nullptr);}catch(const Stop&){}
    assert(memsSnapshot.timestamp>watcherBefore);
    CBuffer wrapUp;
    sensorWaveforms.emit(&wrapUp);
    assert(!sensorWaveforms.hasPending());
    assert(wrapUp.voltageRecords==0 && wrapUp.motionTimestamps==0);
    std::cout<<"PASS: persistent I2C failure retries initialisation, preserves raw motion only on successful reads and continues passive voltage capture\n";
}
'''
with tempfile.TemporaryDirectory(prefix='freematics-mems-') as directory:
    cpp,binary=Path(directory)/'mems.cpp',Path(directory)/'mems'
    cpp.write_text(code)
    subprocess.run(['g++','-std=c++17','-I',str(ROOT),str(cpp),'-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)
