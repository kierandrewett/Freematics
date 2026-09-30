#!/usr/bin/env python3
"""Run production GPS processing with repeat, outage and missing-position fixes."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'telelogger.ino').read_text()

def extract(signature):
    start = source.index(signature + '\n{')
    opening = source.index('{', start)
    end, depth = opening + 1, 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]

base = (ROOT / 'lib/FreematicsPlus/FreematicsBase.h').read_text()
code = r'''
#include <cstdint>
#include <cstdio>
#include <cmath>
#include <map>
#include <cassert>
#include <iostream>
using byte=uint8_t;
using std::abs;
using std::isfinite;
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
#define DEG_TO_RAD (3.14159265358979323846f / 180)
#define STATE_GPS_ONLINE 1
#define ELEMENT_UINT8 0
#define ELEMENT_UINT16 1
#define ELEMENT_UINT32 2
#define ELEMENT_FLOAT 4
#define ELEMENT_FLOAT_D1 5
uint32_t tick=1000;
uint32_t millis() {return tick;}
struct GPS_DATA { uint32_t ts,date,time; float lat,lng,alt,speed; uint16_t heading; uint8_t sat,hdop; };
GPS_DATA gpsSample{},gpsSnapshot{},*gd=nullptr;
int sensorMux;
char isoTime[64];
float tripDistanceKm=0;
uint32_t lastGPSDistanceTime=0,lastMotionTime=0;
struct {void set(int) {}} state;
struct CBuffer {
    std::map<uint16_t,double> values;
    bool add(uint16_t pid, byte type, void* data, int, byte=1) {
        values[pid]=type==ELEMENT_FLOAT || type==ELEMENT_FLOAT_D1 ? *(float*)data :
            type==ELEMENT_UINT8 ? *(uint8_t*)data : type==ELEMENT_UINT16 ? *(uint16_t*)data : *(uint32_t*)data;
        return true;
    }
};
'''
code += '\n'.join(line for line in base.splitlines() if line.startswith('#define PID_')) + '\n'
code += extract('void emitGPSFields(CBuffer* buffer)') + '\n'
code += extract('bool processGPS(CBuffer* buffer)') + '\n'
code += r'''
int main() {
    gpsSnapshot={1000,300926,12000000,51.5f,-.1f,50,20,90,12,1};
    CBuffer initial; assert(processGPS(&initial));
    tick=1250; CBuffer held; processGPS(&held);
    assert(held.values.at(PID_GPS_AGE)==250);
    // A normal position after a 10-second outage exceeds the old fixed bound.
    tick=11000; gpsSnapshot.ts=tick;gpsSnapshot.time=12001000;gpsSnapshot.lat+=.002f;
    CBuffer resumed; processGPS(&resumed);
    assert(resumed.values.count(PID_GPS_LATITUDE));
    assert(resumed.values.at(PID_GPS_AGE)==0);
    // Time and receiver quality remain recorded before the first position fix.
    tick=12000;gpsSnapshot.ts=tick;gpsSnapshot.time=12001100;gpsSnapshot.lat=gpsSnapshot.lng=0;gpsSnapshot.sat=0;
    CBuffer noPosition; processGPS(&noPosition);
    assert(noPosition.values.count(PID_GPS_DATE) && noPosition.values.count(PID_GPS_TIME));
    assert(noPosition.values.count(PID_GPS_SAT_COUNT) && noPosition.values.count(PID_GPS_HDOP));
    assert(!noPosition.values.count(PID_GPS_LATITUDE));
    // Standby callers have no buffer.
    emitGPSFields(nullptr);
    std::cout<<"PASS: GPS cached age, resumed position, no-fix clock/quality, null-buffer caller\n";
}
'''
with tempfile.TemporaryDirectory(prefix='freematics-gps-') as directory:
    cpp, binary = Path(directory)/'gps.cpp',Path(directory)/'gps'
    cpp.write_text(code)
    subprocess.run(['g++','-std=c++17',str(cpp),'-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)
