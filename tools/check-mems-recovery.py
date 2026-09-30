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
#include <iostream>
using byte=uint8_t;
#define ENABLE_MEMS 1
#define ENABLE_ORIENTATION 0
#define STATE_WORKING 1
#define STATE_MEMS_READY 2
#define portMAX_DELAY 0
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
struct Stop {};
uint32_t tick=1000;
uint32_t millis() {return tick;}
void delay(unsigned duration) {tick+=duration;if(tick>=9000)throw Stop{};}
void xSemaphoreTake(int,int) {}
void xSemaphoreGive(int) {}
void vTaskDelete(void*) {}
int memsMutex,sensorMux;
float accBias[3]={};
struct {unsigned flags=3;bool check(unsigned mask){return (flags&mask)==mask;}
void set(unsigned mask){flags|=mask;}void clear(unsigned mask){flags&=~mask;}} state;
struct {template<class T>void println(T){} } Serial;
struct MEMSSnapshot {float acceleration[3]{},gyro[3]{},compass[3]{},temperature=0;uint32_t timestamp=0;};
MEMSSnapshot memsSnapshot;
struct Sensor {
    unsigned initialisations=0,reads=0;
    bool read(float* acc,float*,float*,float*,void*) {reads++;acc[0]=.5;return initialisations>=2;}
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
    assert(memsSnapshot.acceleration[0]==.5f);
    std::cout<<"PASS: persistent I2C failure retries initialisation and resumes fresh MEMS samples\n";
}
'''
with tempfile.TemporaryDirectory(prefix='freematics-mems-') as directory:
    cpp,binary=Path(directory)/'mems.cpp',Path(directory)/'mems'
    cpp.write_text(code)
    subprocess.run(['g++','-std=c++17',str(cpp),'-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)
