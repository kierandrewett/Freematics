#!/usr/bin/env python3
"""Guard the firmware sampling boundary against synchronous sensor/storage I/O."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'telelogger.ino').read_text()

def function(signature):
    start = source.index(signature + "\n{")
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]

signatures = ['void process()']
if 'void collectSample()' in source:
    signatures += ['void collectSample()']
# These are the sampling-side callees; workers own all blocking acquisition.
signatures += ['bool processGPS(CBuffer* buffer)', 'void processMEMS(CBuffer* buffer)', 'float readVehicleVoltage()']
if 'void emitOBDSnapshot(CBuffer* buffer)' in source:
    signatures += ['void emitOBDSnapshot(CBuffer* buffer)']
body = '\n'.join(function(s) for s in signatures)
forbidden = [r'obd\.(?:readPID|init|getVoltage|readDTC)', r'processOBD\(',
             r'logger\.', r'durableQueue\.append', r'capturePassiveCAN\(',
             r'sys\.gps(?:GetData|End|Begin)', r'nvs_(?:commit|set)', r'durableQueue\.pendingBytes', r'initGPS\(',
             r'cell\.getLocation', r'STATIONARY_TIME_TABLE',
             r'state\.clear\(STATE_WORKING\)']
failures = [p for p in forbidden if re.search(p, body)]
if failures:
    raise SystemExit('FAIL: blocking/adaptive sampler paths: ' + ', '.join(failures))
assert 'vTaskDelayUntil' in function('void process()'), 'Sampler needs a fixed deadline'
print('PASS: sampler boundary excludes OBD/GNSS acquisition, reconnect, storage and stationary throttling')

# Run the actual snapshot serializer, queue handoff and absolute scheduler with
# deterministic host I/O. This checks much more than the source boundary guard.
import subprocess
import tempfile


def extract(text, signature):
    start = text.index(signature + '\n{')
    opening = text.index('{', start)
    end, depth = opening + 1, 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


client = (ROOT / 'teleclient.cpp').read_text()
storage = (ROOT / 'telestore.cpp').read_text()
client_header = (ROOT / 'teleclient.h').read_text()
storage_header = (ROOT / 'telestore.h').read_text()
config = (ROOT / 'config.h').read_text()
base = (ROOT / 'lib/FreematicsPlus/FreematicsBase.h').read_text()
code = r'''
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <cstdio>
#include <cassert>
#include <iostream>
#include <algorithm>
using byte=uint8_t;
using TickType_t=uint32_t;
using portMUX_TYPE=int;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
#define BOARD_HAS_PSRAM 0
#define pdMS_TO_TICKS(x) (x)
#define heap_caps_free free
struct SerialStub { template<class T> void println(T) {} void write(uint8_t*,byte) {} void write(char) {} } Serial;
uint32_t tick=1000;
uint32_t millis() {return tick;}
TickType_t xTaskGetTickCount() {return tick;}
void vTaskDelayUntil(TickType_t* deadline, TickType_t interval) { *deadline+=interval; if(tick<*deadline)tick=*deadline; }
int32_t dataInterval=0;
void processBLE(int) {}
uint32_t formattingMs=17;
void collectSample() {tick+=formattingMs;} // formatting work, no OBD/storage I/O
'''
code += '\n'.join(line for line in base.splitlines() if line.startswith('#define PID_') or line.startswith('#define DTC_CODE')) + '\n'
code += '\n'.join(line for line in client_header.splitlines() if line.startswith('#define ')) + '\n'
# Host exercises the complete full-size device queue/buffer, without allocating
# the entire PSRAM inventory. Allocation is tested separately by the build.
code += '#define BUFFER_SLOTS 3\n#define BUFFER_LENGTH 2048\n'
code += '\n'.join(line for line in config.splitlines() if line.startswith('#define SAMPLE_')) + '\n'
code += storage_header[storage_header.index('class CStorage {'):storage_header.index('class FileLogger')]
code += client_header[client_header.index('typedef struct {'):client_header.index('class TeleClient\n')]
code += "#define DTC_STATUS_NO_RESPONSE 0\n#define PID_SPEED 0x0D\n#define PID_RPM 0x0C\n"
code += source[source.index('typedef struct {'):source.index('CBufferManager bufman;')]
code += source[source.index('struct OBDSnapshot {'):source.index('OBDSnapshot obdSnapshot = {};')+len('OBDSnapshot obdSnapshot = {};')]
code += '''\nint sensorMux; uint32_t lastMotionTime=0; void updateOBDDistance(float) {}\n'''
for signature in ['CBuffer::CBuffer(uint8_t* mem)', 'bool CBuffer::add(uint16_t pid, uint8_t type, void* values, int bytes, uint8_t count)',
                  'void CBuffer::purge()', 'void CBuffer::serialize(CStorage& store)', 'void CBufferManager::init()',
                  'CBuffer* CBufferManager::getFree()', 'CBuffer* CBufferManager::getOldest(bool recorded)',
                  'void CBufferManager::free(CBuffer* slot)', 'void CBufferManager::publish(CBuffer* slot)',
                  'void CBufferManager::restore(CBuffer* slot)', 'uint16_t CBufferManager::recordedReadings() const']:
    code += extract(client, signature) + '\n'
# Definitions with default arguments must omit defaults outside the class.
code = code.replace('int bytes, uint8_t count = 1)\n{', 'int bytes, uint8_t count)\n{')
for signature in ['void CStorage::log(uint16_t pid, uint8_t values[], uint8_t count)',
                  'void CStorage::log(uint16_t pid, uint16_t values[], uint8_t count)',
                  'void CStorage::log(uint16_t pid, uint32_t values[], uint8_t count)',
                  'void CStorage::log(uint16_t pid, int32_t values[], uint8_t count)',
                  'void CStorage::log(uint16_t pid, float values[], uint8_t count, const char* fmt)',
                  'void CStorage::logHex(uint16_t pid, const uint8_t values[], uint8_t count)',
                  'void CStorage::timestamp(uint32_t ts)', 'void CStorage::dispatch(const char* buf, byte len)',
                  'void CStorageRAM::dispatch(const char* buf, byte len)', 'byte CStorage::checksum(const char* data, int len)',
                  'void CStorageRAM::header(const char* devid)', 'void CStorageRAM::tailer()']:
    code += extract(storage, signature) + '\n'
code += extract(client, 'void CBufferManager::recordMissedReading(uint32_t count)') + '\n'
code += extract(client, 'uint32_t CBufferManager::missedReadings() const') + '\n'
code += 'CBufferManager bufman;\n'
code += function('void emitOBDSnapshot(CBuffer* buffer)') + '\n'
code += function('void process()') + '\n'
code += r'''
int main() {
  CBufferManager queue; queue.init();
  auto* sample=queue.getFree(); sample->timestamp=100;
  queue.publish(sample);
  assert(!queue.getOldest(true)); // uploader cannot race past recorder
  assert(queue.getOldest(false)==sample);
  assert(!queue.getOldest(false)); // locked custody is exclusive
  sample->recorded=true; queue.restore(sample);
  assert(queue.recordedReadings()==1);
  assert(queue.getOldest(true)==sample);
  queue.restore(sample); // failed POST retains the sample
  assert(queue.getOldest(true)==sample);
  queue.free(sample);
  auto* reused=queue.getFree(); assert(reused==sample && !reused->recorded);
  queue.publish(reused);
  for(int i=0;i<2;i++){auto* item=queue.getFree();item->timestamp=101+i;queue.publish(item);}
  assert(!queue.getFree()); // full queue never overwrites retained samples
  std::cout<<"PASS: recorder/upload custody, failed POST retention, slot reuse, queue saturation\n";

  // Every tracked OBD PID, maximum diagnostic inventory and large ages.
  std::memcpy(obdSnapshot.readings,obdData,sizeof(obdData));
  std::memcpy(obdSnapshot.diagnostics,dtcData,sizeof(dtcData));
  for(auto& item:obdSnapshot.readings){item.ts=1;item.value=65535.99f;}
  for(auto& item:obdSnapshot.diagnostics){item.lastScan=1;item.status=2;item.count=DTC_CODE_SLOTS; for(auto& code:item.codes)code=65535;}
  tick=4000000000u;
  uint8_t memory[BUFFER_LENGTH]; CBuffer rich(memory);
  emitOBDSnapshot(&rich);
  // Include every remaining standard sampler field at conservative sizes.
  float values[3]={99999.99f,-99999.99f,99999.99f};
  for(uint16_t pid: {PID_ACC,PID_GYRO,PID_COMPASS,PID_ORIENTATION}) assert(rich.add(pid,ELEMENT_FLOAT_D2,values,sizeof(values),3));
  uint32_t scalar=0xffffffff;
  for(uint16_t pid: {PID_GPS_DATE,PID_GPS_TIME,PID_GPS_LATITUDE,PID_GPS_LONGITUDE,PID_GPS_ALTITUDE,
      PID_GPS_SPEED,PID_GPS_HEADING,PID_GPS_SAT_COUNT,PID_GPS_HDOP,PID_GPS_AGE,PID_CSQ,PID_CSQ_AGE,
      PID_NETWORK_TRANSPORT,PID_BATTERY_VOLTAGE,PID_VOLTAGE_AGE,PID_MEMS_AGE,PID_TRIP_DISTANCE,
      PID_DEVICE_TEMP,PID_QUEUE_READINGS,PID_QUEUE_BYTES,PID_MISSED_READINGS,PID_DURABLE_QUEUE_BYTES,PID_DURABLE_QUEUE_HEALTH})
      assert(rich.add(pid,ELEMENT_UINT32,&scalar,sizeof(scalar)));
  assert(rich.total>255); // validates widened element count
  char frame[SAMPLE_FRAME_SIZE]; CStorageRAM store;store.init(frame,sizeof(frame));
  store.timestamp(tick);rich.serialize(store);
  assert(!store.overflowed());
  std::string encoded(frame,store.length());
  assert(encoded.find("40C:3999999999,")!=std::string::npos); // stale RPM remains explicit
  std::cout<<"PASS: complete catalogue + ages + DTCs: "<<rich.total<<" fields, "<<rich.offset<<" RAM bytes, "<<store.length()<<" wire bytes\n";
  tick=1000;
  for(int i=0;i<2400;i++){uint32_t before=tick;process();assert(tick-before==250);}
  std::cout<<"PASS: 2400 fixed 250ms deadlines without accumulated 17ms formatting drift\n";
  formattingMs=600;process();assert(bufman.missedReadings()==2);
  std::cout<<"PASS: sampling overload records skipped deadlines rather than fabricating catch-up readings\n";
}
'''
with tempfile.TemporaryDirectory(prefix='freematics-sampling-') as directory:
    cpp, binary = Path(directory) / 'sampling.cpp', Path(directory) / 'sampling'
    cpp.write_text(code)
    subprocess.run(['g++', '-std=c++17', '-I', str(ROOT), str(cpp), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
