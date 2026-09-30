#!/usr/bin/env python3
"""Run the actual firmware wake and alarm functions with deterministic I/O."""

from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def function(text: str, signature: str) -> str:
    start = text.index(signature)
    opening = text.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


source = (ROOT / "telelogger.ino").read_text()
config = (ROOT / "config.h").read_text()
defines = "\n".join(line for line in config.splitlines() if line.startswith("#define ") and
                    any(line.startswith(f"#define {name} ") for name in (
                        "MOTION_THRESHOLD", "STANDBY_MOTION_THRESHOLD", "STANDBY_MOTION_CONFIRM_SAMPLES",
                        "STANDBY_POLL_INTERVAL_MS", "SERVER_RESPONSE_ALERT_MS", "NETWORK_ALERT_GRACE_MS",
                        "NETWORK_ALERT_MIN_INTERVAL_MS", "RECORDING_ALERT_REPEAT_MS",
                        "RECORDING_STALL_ALERT_MS", "JUMPSTART_VOLTAGE", "IGNITION_WAKE_VOLTAGE",
                        "IGNITION_WAKE_CONFIRM_SAMPLES", "OBD_WAKE_POLL_MS",
                        "TRIP_STOP_DELAY_MS", "TRIP_SPEED_FRESH_MS", "TRIP_MOVING_SPEED_KPH",
                        "STANDBY_AFTER_STATIONARY_MS")))
support = "\n".join(function(source, signature) for signature in (
    "float readVehicleVoltage()", "bool vehiclePowerPresent()") if signature in source)
harness = r'''
#include <cstdint>
#include <cmath>
#include <iostream>
#include <stdexcept>
#include <string>
using byte = uint8_t;
using std::isfinite;
#define ENABLE_MEMS 1
#define ENABLE_OBD 1
#define ENABLE_HTTPD 0
#define STORAGE 2
#define STORAGE_SD 2
#ifndef ENABLE_AUDIBLE_SERVER_ALERTS
#define ENABLE_AUDIBLE_SERVER_ALERTS 1
#endif
#define ENABLE_AUDIBLE_NETWORK_ALERTS 0
#ifndef SERVER_PROTOCOL
#define SERVER_PROTOCOL 3
#endif
#define PROTOCOL_HTTPS_POST 3
#define STATE_MEMS_READY 8
#define STATE_STANDBY 512
#define STATE_WORKING 256
#define STATE_NET_READY 16
#define STATE_CELL_CONNECTED 64
#define STATE_WIFI_CONNECTED 128
uint32_t tick = 1000, limit = 90000;
float voltage = 0, movement = 0;
int sensorMux = 0; float cachedVoltage = 0;
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
bool sensorOK = true;
bool loginReplies = false;
bool dataReplies = false, collectReplies = false;
bool storageHealthy = true, storageCheckComplete = true;
bool recoverStorage = false, enterStandby = false;
uint32_t firstToneAt = 0, recoveryAt = 0;
unsigned tonesAfterRecovery = 0;
int tones = 0, startTones = 0, stopTones = 0;
uint32_t firstStopAt = 0;
bool moving = false, speedKnown = true;
int motionMode = 0;
#define PID_SPEED 13
struct Reading { byte pid; float value; uint32_t ts; };
Reading obdData[1] = {{PID_SPEED,0,0}};
using PID_POLLING_INFO = Reading;
struct OBDSnapshot { Reading readings[1]; uint8_t status; } obdSnapshot;
struct GPS_DATA { uint32_t ts; float speed,lat,lng; byte sat,hdop; } gpsSnapshot;

uint32_t lastCollectionTime = 0;
struct Finished {};
uint32_t millis() { return tick; }
void onTick();
void delay(int ms) { tick += ms; onTick(); if (tick > limit) throw Finished{}; }
void esp_sleep_enable_timer_wakeup(uint64_t) {}
void esp_light_sleep_start() { delay(250); }
void processBLE(int) {}
void serverProcess(int ms) { delay(ms > 0 ? ms : 1); }
struct SerialStub {
 template <typename T> void print(T) {}
 template <typename T> void println(T) {}
} Serial;
struct State { unsigned flags; bool check(unsigned f) { return (flags & f) == f; } } state;
struct MEMS { bool read(float* a) { a[0]=movement; a[1]=0; a[2]=1; if (!sensorOK) delay(1); return sensorOK; } } sensor;
MEMS* mems = &sensor;
struct System { int devType = 14; } sys;
struct OBD { float getVoltage() { return voltage; } bool probe = false; int probes = 0;
 void leaveLowPowerMode() {} void enterLowPowerMode() {}
 bool readPID(int,int&) {probes++; return probe;}
} obd;
#define PID_RPM 12
#define PROTO_AUTO 0
#define A0 0
int analogRead(int) { return std::lround(voltage * 4095 / 45); }
float accBias[3]={0,0,1}, accSum[3]={0}; uint8_t accCount=0;
struct Client { uint32_t lastSyncTime=0, lastDataSyncTime=0; } teleClient;
void onTick() {
 if (motionMode==1) moving=tick<10000 || tick>=30000;
 if (motionMode==2) moving=tick<10000 || tick>=120000;
 if (motionMode==3 && tick>=10000) speedKnown=false;
 obdSnapshot.status=speedKnown;
 obdSnapshot.readings[0]={PID_SPEED,moving ? 20.f : 0.f,speedKnown ? tick : 0};
 if (loginReplies) teleClient.lastSyncTime=tick;
 if (dataReplies) teleClient.lastDataSyncTime=tick;
 if (collectReplies) lastCollectionTime=tick;
 if (recoverStorage && tick>=12000) {storageHealthy=true; lastCollectionTime=tick;}
 if (enterStandby && tick>=8000) state.flags=STATE_STANDBY;
}
void beepTone(unsigned frequency, int duration) {
 if (frequency==1200 || frequency==1800) {startTones++; delay(duration); return;}
 if (frequency==1000 || frequency==700) {if (!firstStopAt) firstStopAt=tick; stopTones++; delay(duration); return;}
 if (!firstToneAt) firstToneAt=tick;
 if (recoveryAt && tick>=recoveryAt) tonesAfterRecovery++;
 tones++; delay(duration);
}
uint32_t lastMotionTime = 0; unsigned ramOnly = 0; uint32_t sdBacklog = 0;
struct Buffers { unsigned missedReadings() { return 0; } unsigned unpersistedReadings() { return ramOnly; } } bufman;
struct Storage { bool healthy() {return storageHealthy;} uint32_t cachedPendingBytes() {return sdBacklog;} } durableQueue, logger;
'''
harness += defines + "\n" + support + "\n"
harness += function(source, "bool readTripMotion(bool& moving)") + "\n"
harness += function(source, "void tripChime(bool started)") + "\n"
harness += function(source, "bool waitMotion(long timeout") + "\n"
if "void recordingAlert(const char* message)" in source:
    harness += function(source, "void recordingAlert(const char* message)") + "\n"
harness += function(source, "void statusSignals(void* inst)") + "\n"
harness += function(source, "bool stationaryStandbyDue(uint32_t now)") + "\n"
harness += r'''
int main(int argc, char** argv) {
 std::string scenario=argv[1]; bool woke=false;
 state.flags=STATE_STANDBY|STATE_MEMS_READY;
 if(scenario=="trip-cycle" || scenario=="traffic-light" || scenario=="speed-lost" ||
    scenario=="parked-fault" || scenario=="parked-server") {
   state.flags=STATE_WORKING|STATE_NET_READY|STATE_CELL_CONNECTED;
   moving=scenario!="parked-fault" && scenario!="parked-server";
   collectReplies=true; lastCollectionTime=tick;
   dataReplies=scenario!="parked-server";
   storageHealthy=scenario!="parked-fault";
   motionMode=scenario=="traffic-light" ? 1 : scenario=="trip-cycle" ? 2 : scenario=="speed-lost" ? 3 : 0;
   limit=150000; onTick();
   try {statusSignals(nullptr);} catch(Finished&) {}
   bool pass=tones==0 && (scenario=="trip-cycle" ? startTones==4 && stopTones==2 && firstStopAt>=100000 && firstStopAt<100100 :
      scenario=="traffic-light" || scenario=="speed-lost" ? startTones==2 && stopTones==0 : startTones==0 && stopTones==0);
   std::cout<<scenario<<": alerts="<<tones<<" start="<<startTones<<" stop="<<stopTones<<" "<<(pass?"PASS":"FAIL")<<"\n";
   return !pass;
 }
 if(scenario=="motion-source") {
   bool detected=false;
   onTick(); bool pass=readTripMotion(detected) && !detected;
   moving=true; onTick(); pass &= readTripMotion(detected) && detected;
   tick+=4000; pass &= !readTripMotion(detected);
   gpsSnapshot={tick,10,51,-1,8,2}; pass &= readTripMotion(detected) && detected;
   gpsSnapshot.hdop=20; pass &= !readTripMotion(detected);
   moving=false; onTick(); pass &= readTripMotion(detected) && !detected;
   std::cout<<scenario<<": "<<(pass?"PASS":"FAIL")<<"\n"; return !pass;
 }
 if(scenario.rfind("standby-",0)==0 && scenario!="standby-quiet") {
   // Parked entry uses the real stationaryStandbyDue() decision.
   const uint32_t after=STANDBY_AFTER_STATIONARY_MS;
   voltage=12.4; lastMotionTime=1000; bool pass=true;
   if(scenario=="standby-engine-off") {
     pass=!stationaryStandbyDue(lastMotionTime+after-1) && stationaryStandbyDue(lastMotionTime+after);
   } else if(scenario=="standby-engine-idle") {
     // Fresh RPM refreshes lastMotionTime at the 250 ms sample rate for 10 minutes.
     for(uint32_t t=1000; t<601000 && pass; t+=250) {lastMotionTime=t; pass=!stationaryStandbyDue(t+250);}
   } else if(scenario=="standby-ram-only") {
     ramOnly=1; pass=!stationaryStandbyDue(lastMotionTime+after);
     ramOnly=0; pass&=stationaryStandbyDue(lastMotionTime+after);
   } else if(scenario=="standby-usb-backlog") {
     voltage=5; sdBacklog=4096; pass=!stationaryStandbyDue(lastMotionTime+after);
     voltage=12.4; pass&=stationaryStandbyDue(lastMotionTime+after);
     voltage=5; sdBacklog=0; pass&=stationaryStandbyDue(lastMotionTime+after);
   } else if(scenario=="standby-clock-rollover") {
     lastMotionTime=0xFFFFF000u; pass=!stationaryStandbyDue(lastMotionTime+1000) &&
       stationaryStandbyDue(lastMotionTime+after);
   } else pass=false;
   std::cout<<scenario<<": "<<(pass?"PASS":"FAIL")<<"\n"; return !pass;
 }
 if(scenario=="ignition") voltage=14.4;
 if(scenario=="normal-motion") movement=.12;
 if(scenario=="sensor-failed") {voltage=14.4; sensorOK=false;}
 if(scenario=="sensor-absent") {voltage=14.4; mems=nullptr; state.flags=STATE_STANDBY;}
 if(scenario=="parked") {voltage=12.4; movement=.02;}
 if(scenario=="ecu-probe-suppressed") {voltage=12.4; obd.probe=true;}
 if(scenario=="sd-fault" || scenario=="recording-stall" || scenario=="startup-stall" ||
    scenario=="recording-recovery" || scenario=="fault-standby" || scenario=="healthy-recorder") {
   state.flags=STATE_WORKING|STATE_NET_READY|STATE_CELL_CONNECTED;
   moving=true; onTick();
   dataReplies=true; teleClient.lastDataSyncTime=tick;
   storageHealthy=scenario=="recording-stall" || scenario=="healthy-recorder";
   collectReplies=scenario=="sd-fault" || scenario=="healthy-recorder";
   storageCheckComplete=scenario!="startup-stall";
   lastCollectionTime=scenario=="startup-stall" ? 0 : tick;
   recoverStorage=scenario=="recording-recovery";
   enterStandby=scenario=="fault-standby";
   if(recoverStorage) recoveryAt=12000;
   limit=scenario=="recording-stall" || scenario=="startup-stall" ? 27000 : 23000;
   try {statusSignals(nullptr);} catch(Finished&) {}
   bool pass=scenario=="healthy-recorder" ? tones==0 :
     recoverStorage ? tones>=6 && tonesAfterRecovery==0 :
     (enterStandby ? tones==6 && stopTones==2 : tones>=9) && firstToneAt<=17000;
   std::cout<<scenario<<": tones="<<tones<<" first_ms="<<firstToneAt
     <<" after_recovery="<<tonesAfterRecovery<<" "<<(pass?"PASS":"FAIL")<<"\n";return !pass;
 }
 if(scenario=="login-only" || scenario=="standby-quiet") {
   state.flags=scenario=="login-only" ? STATE_WORKING|STATE_NET_READY|STATE_WIFI_CONNECTED : STATE_STANDBY;
   moving=scenario=="login-only"; onTick();
   loginReplies=true; teleClient.lastSyncTime=tick;
   collectReplies=scenario=="login-only";
   if(collectReplies) lastCollectionTime=tick;
   try {statusSignals(nullptr);} catch(Finished&) {}
   bool pass=scenario=="login-only" ? tones==3 : tones==0;
   std::cout<<scenario<<": tones="<<tones<<" "<<(pass?"PASS":"FAIL")<<"\n";return !pass;
 }
 limit=20000;
 try {woke=waitMotion(-1,STANDBY_MOTION_THRESHOLD,STANDBY_MOTION_CONFIRM_SAMPLES);} catch(Finished&) {}
 bool pass=(scenario=="parked" || scenario=="ignition" || scenario=="ecu-probe-suppressed" ? !woke : woke) && obd.probes==0;
 std::cout<<scenario<<": woke="<<woke<<" time_ms="<<tick<<" "<<(pass?"PASS":"FAIL")<<"\n";
 return !pass;
}
'''
with tempfile.TemporaryDirectory(prefix="freematics-lifecycle-") as directory:
    cpp = Path(directory) / "lifecycle.cpp"
    binary = Path(directory) / "lifecycle"
    cpp.write_text(harness)
    subprocess.run(["g++", "-std=c++17", "-Wall", str(cpp), "-o", str(binary)], check=True)
    failures = 0
    for scenario in ("ignition", "normal-motion", "sensor-failed", "sensor-absent", "parked", "ecu-probe-suppressed",
                     "motion-source", "trip-cycle", "traffic-light", "speed-lost", "parked-fault", "parked-server",
                     "login-only", "standby-quiet", "standby-engine-off", "standby-engine-idle",
                     "standby-ram-only", "standby-usb-backlog", "standby-clock-rollover", "sd-fault", "recording-stall",
                     "startup-stall", "recording-recovery", "fault-standby", "healthy-recorder"):
        failures += subprocess.run([str(binary), scenario], check=False).returncode
    # Moving recording failures remain audible without server alerts or POST.
    subprocess.run(["g++", "-std=c++17", "-Wall", "-DENABLE_AUDIBLE_SERVER_ALERTS=0",
                    "-DSERVER_PROTOCOL=1", str(cpp), "-o", str(binary)], check=True)
    for scenario in ("sd-fault", "recording-stall", "startup-stall", "recording-recovery",
                     "fault-standby", "healthy-recorder", "standby-quiet"):
        failures += subprocess.run([str(binary), scenario], check=False).returncode
    raise SystemExit(bool(failures))
