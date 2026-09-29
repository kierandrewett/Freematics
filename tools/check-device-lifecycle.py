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
                        "IGNITION_WAKE_CONFIRM_SAMPLES", "OBD_WAKE_POLL_MS")))
support = "\n".join(function(source, signature) for signature in (
    "float readVehicleVoltage()", "bool vehiclePowerPresent()") if signature in source)
harness = r'''
#include <cstdint>
#include <cmath>
#include <iostream>
#include <stdexcept>
#include <string>
using byte = uint8_t;
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
bool sensorOK = true;
bool loginReplies = false;
bool dataReplies = false, collectReplies = false;
bool storageHealthy = true, storageCheckComplete = true;
bool recoverStorage = false, enterStandby = false;
uint32_t firstToneAt = 0, recoveryAt = 0;
unsigned tonesAfterRecovery = 0;
int tones = 0;
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
struct OBD { float getVoltage() { return voltage; } bool probe = false;
 void leaveLowPowerMode() {} void enterLowPowerMode() {}
 bool readPID(int,int&) {return probe;}
} obd;
#define PID_RPM 12
#define PROTO_AUTO 0
#define A0 0
int analogRead(int) { return std::lround(voltage * 4095 / 45); }
float accBias[3]={0,0,1}, accSum[3]={0}; uint8_t accCount=0;
struct Client { uint32_t lastSyncTime=0, lastDataSyncTime=0; } teleClient;
void onTick() {
 if (loginReplies) teleClient.lastSyncTime=tick;
 if (dataReplies) teleClient.lastDataSyncTime=tick;
 if (collectReplies) lastCollectionTime=tick;
 if (recoverStorage && tick>=12000) {storageHealthy=true; lastCollectionTime=tick;}
 if (enterStandby && tick>=8000) state.flags=STATE_STANDBY;
}
void beepTone(unsigned, int duration) {
 if (!firstToneAt) firstToneAt=tick;
 if (recoveryAt && tick>=recoveryAt) tonesAfterRecovery++;
 tones++; delay(duration);
}
struct Buffers { unsigned missedReadings() { return 0; } unsigned unpersistedReadings() { return 0; } } bufman;
struct Storage { bool healthy() {return storageHealthy;} } durableQueue, logger;
'''
harness += defines + "\n" + support + "\n"
harness += function(source, "bool waitMotion(long timeout") + "\n"
if "void recordingAlert(const char* message)" in source:
    harness += function(source, "void recordingAlert(const char* message)") + "\n"
harness += function(source, "void statusSignals(void* inst)") + "\n"
harness += r'''
int main(int argc, char** argv) {
 std::string scenario=argv[1]; bool woke=false;
 state.flags=STATE_STANDBY|STATE_MEMS_READY;
 if(scenario=="ignition") voltage=14.4;
 if(scenario=="normal-motion") movement=.12;
 if(scenario=="sensor-failed") {voltage=14.4; sensorOK=false;}
 if(scenario=="sensor-absent") {voltage=14.4; mems=nullptr; state.flags=STATE_STANDBY;}
 if(scenario=="parked") {voltage=12.4; movement=.02;}
 if(scenario=="sd-fault" || scenario=="recording-stall" || scenario=="startup-stall" ||
    scenario=="recording-recovery" || scenario=="fault-standby" || scenario=="healthy-recorder") {
   state.flags=STATE_WORKING|STATE_NET_READY|STATE_CELL_CONNECTED;
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
     tones>=9 && firstToneAt<=16050;
   std::cout<<scenario<<": tones="<<tones<<" first_ms="<<firstToneAt
     <<" after_recovery="<<tonesAfterRecovery<<" "<<(pass?"PASS":"FAIL")<<"\n";return !pass;
 }
 if(scenario=="login-only" || scenario=="standby-quiet") {
   state.flags=scenario=="login-only" ? STATE_WORKING|STATE_NET_READY|STATE_WIFI_CONNECTED : STATE_STANDBY;
   loginReplies=true; teleClient.lastSyncTime=tick;
   collectReplies=scenario=="login-only";
   if(collectReplies) lastCollectionTime=tick;
   try {statusSignals(nullptr);} catch(Finished&) {}
   bool pass=scenario=="login-only" ? tones==3 : tones==0;
   std::cout<<scenario<<": tones="<<tones<<" "<<(pass?"PASS":"FAIL")<<"\n";return !pass;
 }
 limit=6000;
 try {woke=waitMotion(-1,STANDBY_MOTION_THRESHOLD,STANDBY_MOTION_CONFIRM_SAMPLES);} catch(Finished&) {}
 bool pass=scenario=="parked" ? !woke : woke;
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
    for scenario in ("ignition", "normal-motion", "sensor-failed", "sensor-absent", "parked",
                     "login-only", "standby-quiet", "sd-fault", "recording-stall",
                     "startup-stall", "recording-recovery", "fault-standby", "healthy-recorder"):
        failures += subprocess.run([str(binary), scenario], check=False).returncode
    # Local recording failure must remain audible without server alerts or POST.
    subprocess.run(["g++", "-std=c++17", "-Wall", "-DENABLE_AUDIBLE_SERVER_ALERTS=0",
                    "-DSERVER_PROTOCOL=1", str(cpp), "-o", str(binary)], check=True)
    for scenario in ("sd-fault", "recording-stall", "startup-stall", "recording-recovery",
                     "fault-standby", "healthy-recorder", "standby-quiet"):
        failures += subprocess.run([str(binary), scenario], check=False).returncode
    raise SystemExit(bool(failures))
