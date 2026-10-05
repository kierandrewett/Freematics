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
                        "STANDBY_AFTER_STATIONARY_MS", "HTTP_BATCH_MAX_SAMPLES", "CONFIRM_WINDOW_MS",
                        "OBD_SILENT_MS", "CAR_OFF_CONFIRM_MS", "UPLOAD_WINDOW_MS", "RESTING_VOLTAGE_MAX",
                        "SAMPLER_STALL_RESTART_MS", "LOW_BATTERY_WAKE_VOLTAGE", "LOW_BATTERY_CONFIRM_SAMPLES",
                        "HTTP_BATCH_MIN_SAMPLES", "HTTP_BATCH_GROW_STEP")))
support = "\n".join(function(source, signature) for signature in (
    "float readVehicleVoltage()", "bool vehiclePowerPresent()") if signature in source)
harness = r'''
#include <cstdint>
#include <cmath>
#include <iostream>
#include <stdexcept>
#include <string>
#include "OTA_SENSOR_ACTIVITY_HEADER"
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
// OTA policy timing has its own host tests; this lifecycle harness isolates
// wake behavior and only needs the reset notification symbol to link.
void noteOtaResetEvent(uint32_t, bool) {}
int sensorMux = 0; float cachedVoltage = 0;
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
struct SemaphoreStub {};
SemaphoreStub memsMutexStorage;
SemaphoreStub* memsMutex = &memsMutexStorage;
#define pdTRUE 1
#define portMAX_DELAY 0x7fffffff
#define pdMS_TO_TICKS(milliseconds) (milliseconds)
int xSemaphoreTake(SemaphoreStub*, int) { return pdTRUE; }
void xSemaphoreGive(SemaphoreStub*) {}
bool sensorOK = true;
bool loginReplies = false;
bool dataReplies = false, collectReplies = false;
bool rolloverTest = false;
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
using PID_POLLING_INFO = Reading;
struct VehicleSignals { Reading rpm; Reading speed; uint32_t lastResponse; uint8_t status; } vehicleSignals;
struct GPS_DATA { uint32_t ts; float speed,lat,lng; byte sat,hdop; } gpsSnapshot;

uint32_t lastCollectionTime = 0;
uint32_t lastJournalCommitTime = 0;
bool journalCommitSeen = false;
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
 if (rolloverTest && tick < 20000) limit=20000;
 if (motionMode==1) moving=tick<10000 || tick>=30000;
 if (motionMode==2) moving=tick<10000 || tick>=120000;
 if (motionMode==3 && tick>=10000) speedKnown=false;
 if (motionMode==4) voltage=tick<5000 ? 12.4 : 14.2;
 if (motionMode==5) voltage=tick<8000 ? 11.5 : 14.1;
 if (motionMode==6) voltage=tick<1500 ? 11.6 : 12.5;
 vehicleSignals.status=speedKnown;
 vehicleSignals.speed={PID_SPEED,moving ? 20.f : 0.f,speedKnown ? tick : 0};
 if (loginReplies) teleClient.lastSyncTime=tick;
 if (dataReplies) teleClient.lastDataSyncTime=tick;
 if (collectReplies) {lastCollectionTime=tick; lastJournalCommitTime=tick; journalCommitSeen=true;}
 if (recoverStorage && tick>=12000) {storageHealthy=true; lastCollectionTime=tick; lastJournalCommitTime=tick; journalCommitSeen=true;}
 if (enterStandby && tick>=8000) state.flags=STATE_STANDBY;
}
void beepTone(unsigned frequency, int duration) {
 static int chimeNote = 0;
 if (frequency==2000 || frequency==2600 || frequency==3200) {
   // A chime is three notes: rising 2000-2600-3200 starts, falling ends.
   if (chimeNote==0 && frequency==2000) startTones++;
   if (chimeNote==0 && frequency==3200) {if (!firstStopAt) firstStopAt=tick; stopTones++;}
   chimeNote=(chimeNote+1)%3; delay(duration); return;
 }
 if (!firstToneAt) firstToneAt=tick;
 // Warnings are three tones. An alert that starts before recovery may finish
 // after it; only an alert that starts after recovery is a fault.
 static int alertNote = 0; static uint32_t alertStart = 0;
 if (alertNote==0) alertStart=tick;
 alertNote=(alertNote+1)%3;
 if (recoveryAt && alertStart>=recoveryAt) tonesAfterRecovery++;
 tones++; delay(duration);
}
uint32_t lastMotionTime = 0; unsigned ramOnly = 0; uint32_t sdBacklog = 0;
#define PHASE_CONFIRMING 0
#define PHASE_TRIP 1
#define PHASE_WRAP_UP 2
#define PHASE_STANDBY 3
volatile uint8_t powerPhase = PHASE_TRIP;
#define WAKE_POWER_ON 0
#define WAKE_MOTION 1
#define WAKE_CHARGING 2
#define WAKE_RECOVERED 3
#define WAKE_MAGIC 0x57414B00UL
uint32_t wakeRecord = 0;
struct Restarted {};
uint32_t restartAt = 0;
struct { void restart() { restartAt = tick; throw Restarted{}; } } ESP;
struct Buffers { unsigned missedReadings() { return 0; } unsigned unpersistedReadings() { return ramOnly; } } bufman;
struct Storage { bool healthy() {return storageHealthy;} uint32_t cachedPendingBytes() {return sdBacklog;} } durableQueue, logger;
uint32_t fileid = 1;
'''
harness = harness.replace("OTA_SENSOR_ACTIVITY_HEADER", str(ROOT / "ota_sensor_activity.h"))
harness += defines + "\n" + support + "\n"
harness += function(source, "bool readTripMotion(bool& moving)") + "\n"
harness += function(source, "void tripChime(bool started)") + "\n"
harness += function(source, "bool waitMotion(long timeout") + "\n"
if "void recordingAlert(const char* message)" in source:
    harness += function(source, "void recordingAlert(const char* message)") + "\n"
harness += function(source, "void statusSignals(void* inst)") + "\n"
harness += function(source, "uint8_t nextPowerPhase(uint8_t phase, uint32_t now, uint32_t since, uint32_t lastActivity,\n                       bool activitySeen, uint32_t lastOBDResponse, float voltage,\n                       uint32_t backlogBytes, uint16_t ramOnly)") + "\n"
harness += function(source, "uint8_t adaptBatchLimit(uint8_t limit, bool sent, uint16_t status)") + "\n"
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
   bool pass=tones==0 && (scenario=="trip-cycle" ? startTones==2 && stopTones==1 && firstStopAt>=100000 && firstStopAt<100100 :
      scenario=="traffic-light" || scenario=="speed-lost" ? startTones==1 && stopTones==0 : startTones==0 && stopTones==0);
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
 if(scenario.rfind("phase-",0)==0) {
   // The real nextPowerPhase() decision, one failure mode per scenario.
   auto step=[](uint8_t phase, uint32_t now, uint32_t since, uint32_t activity, bool seen,
                uint32_t obd, float volts, uint32_t backlog=0, uint16_t ram=0) {
     return nextPowerPhase(phase, now, since, activity, seen, obd, volts, backlog, ram); };
   bool pass=true;
   if(scenario=="phase-false-wake") {
     // No engine, no movement: stay confirming (modem off), then sleep at 45 s.
     pass=step(PHASE_CONFIRMING, CONFIRM_WINDOW_MS-1, 0, 0, false, 0, 12.4)==PHASE_CONFIRMING &&
          step(PHASE_CONFIRMING, CONFIRM_WINDOW_MS, 0, 0, false, 0, 12.4)==PHASE_STANDBY;
   } else if(scenario=="phase-real-wake") {
     pass=step(PHASE_CONFIRMING, 5000, 0, 5000, true, 5000, 14.2)==PHASE_TRIP;
   } else if(scenario=="phase-red-light") {
     // Stop-start engine off at a light: ECU still answers, battery voltage.
     for(uint32_t t=1000; t<170000 && pass; t+=250) pass=step(PHASE_TRIP, t, 0, 1000, true, t, 12.3)==PHASE_TRIP;
   } else if(scenario=="phase-ignition-off") {
     // ECU silent from 1 s, last activity at 1 s: wrap-up 15 s later, not 180 s.
     pass=step(PHASE_TRIP, 1000+CAR_OFF_CONFIRM_MS-1, 0, 1000, true, 1000, 12.6)==PHASE_TRIP &&
          step(PHASE_TRIP, 1000+CAR_OFF_CONFIRM_MS, 0, 1000, true, 1000, 12.6)==PHASE_WRAP_UP;
   } else if(scenario=="phase-ecu-dropout-charging") {
     // OBD drops out while the alternator charges: the engine runs, keep going.
     pass=step(PHASE_TRIP, 60000, 0, 1000, true, 1000, 14.1)==PHASE_TRIP;
   } else if(scenario=="phase-wrap-up") {
     // Empty backlog sleeps at once; a backlog gets at most two minutes.
     pass=step(PHASE_WRAP_UP, 1000, 1000, 500, true, 500, 12.6, 0, 0)==PHASE_STANDBY &&
          step(PHASE_WRAP_UP, 1000+UPLOAD_WINDOW_MS-1, 1000, 500, true, 500, 12.6, 50000, 0)==PHASE_WRAP_UP &&
          step(PHASE_WRAP_UP, 1000+UPLOAD_WINDOW_MS, 1000, 500, true, 500, 12.6, 50000, 3)==PHASE_STANDBY;
   } else if(scenario=="phase-wrap-up-resume") {
     pass=step(PHASE_WRAP_UP, 30000, 1000, 29000, true, 29000, 14.0, 50000)==PHASE_TRIP;
   } else if(scenario=="phase-bench") {
     // USB power (no car battery): trip at once, keep uploading the backlog.
     pass=step(PHASE_CONFIRMING, 10, 0, 0, false, 0, 5.0)==PHASE_TRIP &&
          step(PHASE_TRIP, STANDBY_AFTER_STATIONARY_MS+1000, 0, 1000, false, 0, 5.0, 4096)==PHASE_TRIP &&
          step(PHASE_TRIP, STANDBY_AFTER_STATIONARY_MS+1000, 0, 1000, false, 0, 5.0, 0)==PHASE_WRAP_UP;
   } else if(scenario=="phase-no-obd") {
     // GNSS-only car at a light: no ECU answer is not a trip end before 180 s.
     pass=step(PHASE_TRIP, 60000, 0, 1000, true, 0, 12.5)==PHASE_TRIP &&
          step(PHASE_TRIP, 1000+STANDBY_AFTER_STATIONARY_MS, 0, 1000, true, 0, 12.5)==PHASE_WRAP_UP;
   } else if(scenario=="phase-clock-rollover") {
     const uint32_t base=0xFFFFF000u;
     pass=step(PHASE_TRIP, (uint32_t)(base+CAR_OFF_CONFIRM_MS), 0, base, true, base, 12.6)==PHASE_WRAP_UP &&
          step(PHASE_WRAP_UP, base+20000, base+10000, base+15000, true, base+15000, 14.0, 10)==PHASE_TRIP &&
          step(PHASE_CONFIRMING, (uint32_t)(base+CONFIRM_WINDOW_MS), base, 0, false, 0, 12.4)==PHASE_STANDBY;
   } else pass=false;
   std::cout<<scenario<<": "<<(pass?"PASS":"FAIL")<<"\n"; return !pass;
 }
 if(scenario=="batch-adapt") {
   // Weak link: halve per failed request down to the floor. Refused bytes
   // (HTTP 400) do not shrink it. Accepted requests grow it back to the cap.
   uint8_t limit=HTTP_BATCH_MAX_SAMPLES; std::string trace;
   for(int i=0;i<5;i++) {limit=adaptBatchLimit(limit,false,0); trace+=std::to_string(limit)+" ";}
   bool pass=limit==HTTP_BATCH_MIN_SAMPLES && adaptBatchLimit(limit,false,400)==limit;
   for(int i=0;i<20;i++) limit=adaptBatchLimit(limit,true,200);
   pass&=limit==HTTP_BATCH_MAX_SAMPLES;
   std::cout<<scenario<<": shrink "<<trace<<(pass?"PASS":"FAIL")<<"\n"; return !pass;
 }
 if(scenario=="ignition") voltage=14.4;
 if(scenario=="charging-edge") {voltage=12.4; motionMode=4;}
 // Weak battery: someone moves the car while parked, then starts the engine.
 if(scenario=="low-battery-motion") {voltage=11.5; movement=.12;}
 if(scenario=="low-battery-start") {voltage=11.5; movement=.12; motionMode=5;}
 // A short dip (door open, courtesy light) must not latch the guard.
 if(scenario=="low-battery-dip") {voltage=11.6; movement=.12; motionMode=6;}
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
     (enterStandby ? tones==6 && stopTones==1 : tones>=9) && firstToneAt<=17000;
   std::cout<<scenario<<": tones="<<tones<<" first_ms="<<firstToneAt
     <<" after_recovery="<<tonesAfterRecovery<<" "<<(pass?"PASS":"FAIL")<<"\n";return !pass;
 }
 if(scenario=="sampler-stall" || scenario=="wrap-up-pause") {
   // A stalled sampler restarts the device after 60 s and records why. A
   // 150 s wrap-up (sampling paused on purpose) restarts nothing and raises
   // no recording fault, even when the car pulls away again.
   state.flags=STATE_WORKING|STATE_NET_READY|STATE_CELL_CONNECTED;
   moving=false; onTick();
   dataReplies=true; teleClient.lastDataSyncTime=tick;
   storageHealthy=true; storageCheckComplete=true;
   lastCollectionTime=tick;
   if (scenario=="wrap-up-pause") powerPhase=PHASE_WRAP_UP;
   limit=tick+150000;
   bool restarted=false;
   try {statusSignals(nullptr);} catch(Finished&) {} catch(Restarted&) {restarted=true;}
   bool pass=scenario=="sampler-stall"
     ? restarted && restartAt>=1000+SAMPLER_STALL_RESTART_MS && restartAt<1000+SAMPLER_STALL_RESTART_MS+100 &&
       wakeRecord==(WAKE_MAGIC|WAKE_RECOVERED)
     : !restarted && tones==0;
   if (scenario=="wrap-up-pause" && pass) {
     // Pulling away: the sampler refreshes the collection time as it leaves
     // wrap-up, then the car moves. No warning may sound.
     lastCollectionTime=tick; powerPhase=PHASE_TRIP; collectReplies=true; moving=true;
     limit=tick+20000;
     try {statusSignals(nullptr);} catch(Finished&) {} catch(Restarted&) {restarted=true;}
     pass=!restarted && tones==0;
   }
   std::cout<<scenario<<": restarted="<<restarted<<" at_ms="<<restartAt<<" tones="<<tones<<" "<<(pass?"PASS":"FAIL")<<"\n";
   return !pass;
 }
 if(scenario=="journal-rollover") {
   state.flags=STATE_WORKING|STATE_NET_READY|STATE_CELL_CONNECTED;
   moving=true; tick=UINT32_MAX-500; rolloverTest=true; limit=UINT32_MAX;
   dataReplies=true; collectReplies=true; onTick();
   storageHealthy=true; storageCheckComplete=true;
   try {statusSignals(nullptr);} catch(Finished&) {}
   bool pass=journalCommitSeen && tones==0;
   std::cout<<scenario<<": commit_at="<<lastJournalCommitTime<<" tones="<<tones<<" "
     <<(pass?"PASS":"FAIL")<<"\n"; return !pass;
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
 bool pass=(scenario=="parked" || scenario=="ignition" || scenario=="ecu-probe-suppressed" ||
            scenario=="low-battery-motion" ? !woke : woke) && obd.probes==0;
 // A wake records its reason for the next boot; a maintainer at 14.4 V from the start never wakes.
 if (scenario=="charging-edge") pass=pass && tick>=5000 && tick<6500 && wakeRecord==(WAKE_MAGIC|WAKE_CHARGING);
 if (scenario=="low-battery-start") pass=pass && tick>=8000 && tick<9500 && wakeRecord==(WAKE_MAGIC|WAKE_CHARGING);
 if (scenario=="low-battery-dip") pass=pass && wakeRecord==(WAKE_MAGIC|WAKE_MOTION);
 if (scenario=="normal-motion") pass=pass && wakeRecord==(WAKE_MAGIC|WAKE_MOTION);
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
                     "login-only", "standby-quiet", "phase-false-wake", "phase-real-wake", "phase-red-light",
                     "phase-ignition-off", "phase-ecu-dropout-charging", "phase-wrap-up", "phase-wrap-up-resume",
                     "phase-bench", "phase-no-obd", "phase-clock-rollover", "charging-edge", "batch-adapt",
                     "sampler-stall", "wrap-up-pause", "low-battery-motion", "low-battery-start", "low-battery-dip", "journal-rollover", "sd-fault", "recording-stall",
                     "startup-stall", "recording-recovery", "fault-standby", "healthy-recorder"):
        failures += subprocess.run([str(binary), scenario], check=False).returncode
    # Moving recording failures remain audible without server alerts or POST.
    subprocess.run(["g++", "-std=c++17", "-Wall", "-DENABLE_AUDIBLE_SERVER_ALERTS=0",
                    "-DSERVER_PROTOCOL=1", str(cpp), "-o", str(binary)], check=True)
    for scenario in ("sd-fault", "recording-stall", "startup-stall", "recording-recovery",
                     "fault-standby", "healthy-recorder", "standby-quiet"):
        failures += subprocess.run([str(binary), scenario], check=False).returncode
    raise SystemExit(bool(failures))
