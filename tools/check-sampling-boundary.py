#!/usr/bin/env python3
"""Guard the sampling boundary and verify overloads are visible, not fabricated."""
from pathlib import Path
import os
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
signatures += ['bool processGPS(CBuffer* buffer)', 'void processMEMS(CBuffer* buffer)', 'float readVehicleVoltage()',
               'bool vehicleActivityNow(uint32_t now, uint32_t* lastOBDResponse)']
if 'void emitOBDSnapshot(CBuffer* buffer)' in source:
    signatures += ['void emitOBDSnapshot(CBuffer* buffer)']
body = '\n'.join(function(s) for s in signatures)
sampler_functions = '\n'.join(
    function(signature)
    for signature in ('void process()', 'void collectSample()')
    if signature in source
)
assert not re.search(r'\bSerial\.(?:print|println|printf|write)\s*\(', sampler_functions), \
    'Fixed-cadence sampling must not wait on debug/telemetry serial output'
tx_ring_setup = source.index('Serial.setTxBufferSize(USB_TELEMETRY_TX_BUFFER_SIZE)')
serial_begin = source.index('Serial.begin(460800)')
assert tx_ring_setup < serial_begin, 'USB TX ring must be configured before the UART starts'
task_create = source.index('usbTelemetryTask.create')
task_guard = source.rfind('if (sys.devType', 0, task_create)
assert 'usbTelemetrySerialReady &&' in source[task_guard:task_create], \
    'USB streaming must stay disabled if its bounded TX ring failed to initialize'
forbidden = [r'obd\.(?:readPID|init|getVoltage|readDTC)', r'processOBD\(',
             r'durableQueue\.append', r'capturePassiveCAN\(',
             r'sys\.gps(?:GetData|End|Begin)', r'nvs_(?:commit|set)', r'durableQueue\.pendingBytes', r'initGPS\(',
             r'cell\.getLocation', r'STATIONARY_TIME_TABLE', r'dataInterval\s*=\s*dataIntervals']
failures = [p for p in forbidden if re.search(p, body)]
if failures:
    raise SystemExit('FAIL: blocking/adaptive sampler paths: ' + ', '.join(failures))
assert 'vTaskDelayUntil' in function('void process()'), 'Sampler needs a fixed deadline'
# The sampler never slows down while the car is in use. It may pause or stop
# only through nextPowerPhase() (wrap-up or standby after the car turns off),
# which tools/check-device-lifecycle.py exercises.
process_body = function('void process()')
assert re.search(r'if \(next == PHASE_STANDBY\) \{\s*state\.clear\(STATE_WORKING\);', process_body), \
    'Sampler may stop only through nextPowerPhase()'
assert 'if (powerPhase == PHASE_WRAP_UP)' in process_body, 'Sampler may pause only in wrap-up'
assert process_body.count('STATE_WORKING') == 1, 'Unexpected extra sampler state change'
print('PASS: sampler boundary excludes blocking OBD/GNSS acquisition and stationary throttling')

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
#include <fstream>
#include <cmath>
#include <algorithm>
using byte=uint8_t;
using std::isfinite;
using TickType_t=uint32_t;
using portMUX_TYPE=int;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
#define BOARD_HAS_PSRAM 0
#define pdMS_TO_TICKS(x) (x)
#define heap_caps_free free
struct SerialStub { template<class T> void print(T) {} template<class T> void println(T) {} void write(uint8_t*,byte) {} void write(char) {} } Serial;
uint32_t tick=1000;
uint32_t millis() {return tick;}
TickType_t xTaskGetTickCount() {return tick;}
void vTaskDelayUntil(TickType_t* deadline, TickType_t interval) { *deadline+=interval; if(tick<*deadline)tick=*deadline; }
int32_t dataInterval=0;
void processBLE(int) {}
uint32_t formattingMs=17;
void collectSample();
'''
code += '\n'.join(line for line in base.splitlines() if line.startswith('#define PID_') or line.startswith('#define DTC_CODE')) + '\n'
code += '\n'.join(line for line in client_header.splitlines() if line.startswith('#define ')) + '\n'
# Host exercises the complete full-size device queue/buffer, without allocating
# the entire PSRAM inventory. Allocation is tested separately by the build.
code += '#define BUFFER_SLOTS 3\n'
code += re.search(r'^#define BUFFER_LENGTH .*$', config, re.MULTILINE).group(0) + '\n'
code += '\n'.join(line for line in config.splitlines() if line.startswith('#define SAMPLE_')) + '\n'
code += re.search(r'^#define UPLOAD_WINDOW_MS .*$', config, re.MULTILINE).group(0) + '\n'
code += storage_header[storage_header.index('class CStorage {'):storage_header.index('class FileLogger')]
code += client_header[client_header.index('typedef struct {'):client_header.index('class TeleClient\n')]
code += '#include "sensorwaveform.h"\n'
code += '#include "usbtelemetry_metadata.h"\n'
code += '#include "recording_checkpoint_policy.h"\n'
code += '''
SensorWaveforms sensorWaveforms;
freematics::recording::WrapUpCheckpointPolicy wrapUpCheckpointPolicy;
uint32_t journalCommitCount=0;
bool collectionBlocked=false;
unsigned collectionCalls=0;
void collectSample() {
    ++collectionCalls;
    if(!collectionBlocked) {
        uint8_t memory[BUFFER_LENGTH]; CBuffer sample(memory);
        sensorWaveforms.emit(&sample);
        ++journalCommitCount; // deterministic successful journal commit
    }
    tick+=formattingMs;
}
'''
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
code += extract(client, 'uint16_t CBufferManager::unpersistedReadings() const') + '\n'
code += 'CBufferManager bufman;\n'
code += function('void emitOBDSnapshot(CBuffer* buffer)') + '\n'
# The real standby decision is covered by tools/check-device-lifecycle.py.
# Here the car is always in use, so every deadline must produce a sample.
code += '#define STATE_WORKING 256\nstruct { void clear(unsigned) {} } state;\n'
code += '#define PHASE_CONFIRMING 0\n#define PHASE_TRIP 1\n#define PHASE_WRAP_UP 2\n#define PHASE_STANDBY 3\n'
code += 'uint8_t powerPhase = PHASE_TRIP; uint32_t phaseSince = 0; bool vehicleActivitySeen = true;\n'
code += 'uint32_t lastCollectionTime = 0;\n'
code += 'bool vehicleActivityNow(uint32_t, uint32_t* obd) { *obd = millis(); return true; }\n'
code += 'void noteOtaResetEvent(uint32_t, bool) {}\n'
code += 'float readVehicleVoltage() { return 14.2f; }\n'
code += 'struct { uint32_t cachedPendingBytes() { return 0; } } durableQueue;\n'
code += 'uint8_t nextPhase=PHASE_TRIP;\n'
code += 'uint8_t nextPowerPhase(uint8_t, uint32_t, uint32_t, uint32_t, bool, uint32_t, float, uint32_t, uint16_t) { return nextPhase; }\n'
code += function('void accountUnjournaledWaveforms(const CBuffer* buffer)') + '\n'
code += function('void process()') + '\n'
code += r'''
int main(int argc, char** argv) {
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
  // Waveform failure cases: preserve short excursions between frame times,
  // timestamp zero/rollover, both sensors independently, raw signed vectors,
  // invalid inputs, FIFO saturation, and insufficient destination capacity.
  // The checks run the production FIFO, CBuffer and text serializer together.
  SensorWaveforms waveform;
  for (unsigned i=0; i<16; ++i) {
    const float rawAcc[3]={0.001234f, -0.987654f, 1.012345f};
    const float rawGyro[3]={-3999.123f, 12.123456f, 0.000123f};
    assert(waveform.recordVoltage(tick-300+i*20, i==7 ? 9.8f : 14.2f));
    assert(waveform.recordMotion(tick-299+i*20,rawAcc,rawGyro));
  }
  waveform.emit(&rich);
  char frame[SAMPLE_FRAME_SIZE]; CStorageRAM store;store.init(frame,sizeof(frame));
  store.timestamp(tick);rich.serialize(store);store.tailer();
  assert(!store.overflowed());
  std::string encoded(frame,store.length());
  // Include room for the FT1 envelope and newline around this complete body.
  // UART 8N1 carries ten wire bits per byte: the old baud cannot sustain four
  // rich frames/s, while the selected 460800 baud has ample headroom.
  const size_t richFrameBytes = store.length() + 128;
  assert(richFrameBytes * 10 * 4 > 115200);
  assert(richFrameBytes * 10 * 4 < 460800);
  const size_t checksumMarker=encoded.find('*');
  assert(checksumMarker!=std::string::npos);
  for(size_t begin=0;begin<checksumMarker;) {
    size_t end=encoded.find(',',begin);
    if(end==std::string::npos || end>checksumMarker) end=checksumMarker;
    assert(encoded.find(':',begin)<end); // every production-serialized item is PID:value
    if(end==checksumMarker) break;
    begin=end+1;
  }
  assert(encoded.find("40C:3999999999,")!=std::string::npos); // stale RPM remains explicit
  auto occurrences=[](const std::string& text,const std::string& needle) {
    unsigned count=0; size_t position=0;
    while((position=text.find(needle,position))!=std::string::npos){++count;position+=needle.size();}
    return count;
  };
  assert(occurrences(encoded,",A0:")==16 && occurrences(encoded,",A1:")==16);
  assert(encoded.find("A2:0.001234;-0.987654;1.012345,")!=std::string::npos);
  assert(encoded.find("A0:3999999840;980,")!=std::string::npos);
  assert(encoded.find("A4:0;0;0;0,")!=std::string::npos);
  assert(encoded.find("A5:1,")!=std::string::npos);
  std::cout<<"PASS: complete catalogue + ages + DTCs + 16 waveform pairs: "<<rich.total<<" fields, "<<rich.offset<<" RAM bytes, "<<store.length()<<" wire bytes\n";
  if(argc>1){std::ofstream file(argv[1]);file<<encoded;}

  auto serialise=[&](CBuffer& buffer) {
    store.purge(); store.timestamp(20); buffer.serialize(store);
    assert(!store.overflowed()); return std::string(frame,store.length());
  };
  auto drain=[&](SensorWaveforms& waves) {
    rich.purge(); waves.emit(&rich); return serialise(rich);
  };
  const float acceleration[3]={0,0,1}, gyro[3]={0,0,0};
  for(unsigned i=0;i<3;++i) assert(sensorWaveforms.recordVoltage(10+i*20,14));
  for(unsigned i=0;i<2;++i) assert(sensorWaveforms.recordMotion(11+i*20,acceleration,gyro));
  rich.purge(); sensorWaveforms.emit(&rich); // points enter a volatile CBuffer
  assert(rich.waveformVoltageSamples==3 && rich.waveformMotionSamples==2);
  // Model DurableQueue::appendBatch failure: the frame is released, and the
  // next journalable sample must carry loss totals for the emitted points.
  accountUnjournaledWaveforms(&rich);
  const auto afterAppendFailure=drain(sensorWaveforms);
  assert(occurrences(afterAppendFailure,",A0:")==0 && occurrences(afterAppendFailure,",A1:")==0);
  assert(afterAppendFailure.find("A4:3;2;0;0,")!=std::string::npos);
  std::cout<<"PASS: failed SD append counts emitted voltage/motion points in the next frame\n";
  assert(occurrences(drain(waveform),",A0:")==0); // consumed exactly once
  SensorWaveforms wrap;
  for(uint32_t stamp: {0xfffffff0u,0u,20u}) {
    assert(wrap.recordVoltage(stamp,14));
    assert(wrap.recordMotion(stamp,acceleration,gyro));
  }
  rich.purge(); rich.offset=BUFFER_LENGTH-2; // cannot even fit contract fields
  const auto oldTotal=rich.total;
  wrap.emit(&rich);
  assert(rich.total==oldTotal && rich.offset==BUFFER_LENGTH-2);
  std::string wrapEncoded=drain(wrap);
  assert(occurrences(wrapEncoded,",A0:")==3 && occurrences(wrapEncoded,",A1:")==3);
  assert(wrapEncoded.find("A0:4294967280;1400,")!=std::string::npos);
  assert(wrapEncoded.find("A1:0,A2:")!=std::string::npos);
  // Limited space must not emit part of a motion group or consume the FIFO.
  assert(wrap.recordMotion(40,acceleration,gyro));
  rich.purge(); rich.offset=BUFFER_LENGTH-30;
  wrap.emit(&rich);
  assert(drain(wrap).find("A1:40,A2:")!=std::string::npos);
  assert(occurrences(drain(wrap),",A1:")==0);

  SensorWaveforms full;
  for(unsigned i=0;i<128;++i) {
    assert(full.recordVoltage(i*20,14));
    assert(full.recordMotion(i*20,acceleration,gyro));
  }
  assert(!full.recordVoltage(9999,14));
  assert(!full.recordMotion(9999,acceleration,gyro));
  const float invalid[3]={NAN,0,0};
  assert(!full.recordVoltage(10000,NAN));
  assert(!full.recordMotion(10000,invalid,gyro));
  unsigned voltageCount=0,motionCount=0;
  std::string all;
  for(unsigned i=0;i<8;++i) {
    const auto chunk=drain(full);
    assert(occurrences(chunk,",A0:")==16 && occurrences(chunk,",A1:")==16);
    voltageCount+=occurrences(chunk,",A0:");motionCount+=occurrences(chunk,",A1:");all+=chunk;
  }
  assert(voltageCount==128 && motionCount==128);
  assert(all.find("A4:1;1;1;1,")!=std::string::npos);
  assert(all.find("A1:9999,")==std::string::npos);
  assert(drain(full).find("A4:1;1;1;1,")!=std::string::npos);
  assert(full.recordVoltage(11000,12.6f));
  assert(occurrences(drain(full),",A0:")==1);
  assert(full.recordMotion(11020,acceleration,gyro));
  const auto motionOnly=drain(full);
  assert(occurrences(motionOnly,",A0:")==0 && occurrences(motionOnly,",A1:")==1);
  SensorWaveforms noStorage;
  for(unsigned i=0;i<3;++i) assert(noStorage.recordVoltage(12000+i*20,14));
  for(unsigned i=0;i<2;++i) assert(noStorage.recordMotion(12000+i*20,acceleration,gyro));
  noStorage.discardPending();
  const auto discarded=drain(noStorage);
  assert(occurrences(discarded,",A0:")==0 && occurrences(discarded,",A1:")==0);
  assert(discarded.find("A4:3;2;0;0,")!=std::string::npos);
  std::cout<<"PASS: waveform drain exactly once, raw precision, independent sensors, rollover, no partial groups, saturation and invalid-input loss counters\n";
  tick=1000;
  for(int i=0;i<2400;i++){uint32_t before=tick;process();assert(tick-before==250);}
  std::cout<<"PASS: 2400 fixed 250ms deadlines without accumulated 17ms formatting drift\n";
  formattingMs=600;process();assert(bufman.missedReadings()==2);
  std::cout<<"PASS: sampling overload records skipped deadlines rather than fabricating catch-up readings\n";
  formattingMs=17;
  for(unsigned i=0;i<32;++i) assert(sensorWaveforms.recordVoltage(tick+i,14));
  nextPhase=PHASE_WRAP_UP;
  collectionBlocked=true;
  unsigned beforeCalls=collectionCalls;
  process(); // RAM queue unavailable on transition: preserve FIFO, retry later
  assert(sensorWaveforms.hasPending() && collectionCalls==beforeCalls+1);
  collectionBlocked=false;
  process(); assert(sensorWaveforms.hasPending());
  process(); assert(!sensorWaveforms.hasPending());
  beforeCalls=collectionCalls;
  process(); assert(collectionCalls==beforeCalls); // parked state adds no empty samples
  nextPhase=PHASE_TRIP;
  process(); assert(powerPhase==PHASE_TRIP && collectionCalls==beforeCalls+1);
  std::cout<<"PASS: wrap-up retries a blocked final drain, drains pending waveform segments, pauses when empty and resumes collection\n";
}
'''
with tempfile.TemporaryDirectory(prefix='freematics-sampling-') as directory:
    cpp, binary = Path(directory) / 'sampling.cpp', Path(directory) / 'sampling'
    cpp.write_text(code)
    subprocess.run(['g++', '-std=c++17', '-I', str(ROOT), str(cpp), '-o', str(binary)], check=True)
    output = os.environ.get("FREEMATICS_WAVEFORM_FIXTURE_OUT")
    subprocess.run([str(binary), *([output] if output else [])], check=True)
