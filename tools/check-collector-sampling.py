#!/usr/bin/env python3
"""Exercise the real collector HTTP ingestion/API/archive with held readings."""
from pathlib import Path
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('FREEMATICS_COLLECTOR_BINARY', ROOT / 'collector/teleserver'))
AUTH_ARGS = ['-x'] if os.environ.get('FREEMATICS_TEST_PROXY') == '1' else ['-w', 'sampling-fixture-only']


def request(base, path, data=None):
    req = urllib.request.Request(base + path, data=data, headers={'Authorization': 'Bearer ' + 'A' * 64, 'Content-Type': 'application/octet-stream'})
    with urllib.request.urlopen(req, timeout=3) as response:
        return response.read()


def metric_value(metrics, device, pid):
    prefix = f'freematics_obd_value_age_seconds{{device_id="{device}",'
    needle = f'pid="0x{pid:03X}"'
    lines = [line for line in metrics.splitlines() if line.startswith(prefix) and needle in line]
    assert len(lines) == 1, lines
    return float(lines[0].rsplit(' ', 1)[1])


with tempfile.TemporaryDirectory(prefix='freematics-collector-sampling-') as directory:
    root = Path(directory)
    (root / 'data').mkdir()
    (root / 'log').mkdir()
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    with (root / 'server.log').open('wb') as log:
        command = [str(BINARY), '-g', '-p', str(port), '-u', '0', *AUTH_ARGS,
                   '-d', str(root / 'data'), '-l', str(root / 'log')]
        server = subprocess.Popen(command, cwd=root, stdout=log, stderr=log)
        try:
            for _ in range(120):
                try:
                    request(base, '/api/test')
                    break
                except OSError:
                    if server.poll() is not None:
                        raise RuntimeError('Collector exited during startup')
                    time.sleep(.05)
            request(base, '/api/notify/FULLRATE?EV=1&TS=1000')
            for i in range(4):
                text = f'0:{1000 + 250*i},11:290926,10:12101300,93:{250*i},10C:900,40C:{250*i},89:1'
                packet = (text + f'*{sum(text.encode()) & 255:X}').encode()
                result = request(base, '/api/post/FULLRATE', packet)
                assert b'OK' in result, result
            # Real firmware batches repeat PID 0. A lost response retries the
            # same batch; neither acceptance nor retry may split the trip.
            request(base, '/api/notify/BATCHED?EV=1&TS=1000')
            text = ','.join(f'0:{1000+250*i},10C:{900+i},40C:0' for i in range(8))
            packet = (text + f'*{sum(text.encode()) & 255:X}').encode()
            assert request(base, '/api/post/BATCHED', packet).strip() == b'OK 16'
            batch_files = list((root / 'data' / 'BATCHED').rglob('*.txt'))
            assert len(batch_files) == 1, batch_files
            assert request(base, '/api/post/BATCHED', packet).strip() == b'OK 16'
            assert list((root / 'data' / 'BATCHED').rglob('*.txt')) == batch_files
            archived_batch = batch_files[0].read_text()
            assert all(f'0:{1000+250*i},' in archived_batch for i in range(8))
            # A malformed sample is set aside with its reason; the valid sample
            # in the same batch is stored, and the reply counts both, so the
            # device releases the batch instead of resending it forever.
            text = '0:3000,10C:1,0:4294967296,10C:2'
            mixed = (text + f'*{sum(text.encode()) & 255:X}').encode()
            assert request(base, '/api/post/BATCHED', mixed).strip() == b'OK 2'
            archive = ''.join(path.read_text() for path in (root / 'data' / 'BATCHED').rglob('*.txt')
                              if path.name != 'rejected.txt')
            assert '0:3000,10C:1' in archive and '4294967296' not in archive
            rejected = (root / 'data' / 'BATCHED' / 'rejected.txt').read_text()
            assert ' invalid timestamp 0:4294967296,10C:2' in rejected, rejected
            # A reboot inside one batch restarts the device clock. Both halves
            # are stored; the older-clock half starts a new trip archive.
            text = '0:9000,10C:5,40C:0,0:1000,10C:6,40C:0'
            reboot = (text + f'*{sum(text.encode()) & 255:X}').encode()
            assert request(base, '/api/post/BATCHED', reboot).strip() == b'OK 4'
            files = list((root / 'data' / 'BATCHED').rglob('*.txt'))
            archive = ''.join(path.read_text() for path in files if path.name != 'rejected.txt')
            assert '0:9000,10C:5,40C:0' in archive and '0:1000,10C:6,40C:0' in archive
            from urllib.error import HTTPError
            try:
                request(base, '/api/post/BATCHED', b'*00')
            except HTTPError as error:
                assert error.code == 400
            else:
                raise AssertionError('Empty body accepted')
            print('PASS: eight-sample HTTP batch, lost-response retry stays in one archive, malformed sample set aside, reboot inside a batch stored')
            # Compressed batches (?z=1) inflate to the same result. A body that
            # does not inflate is refused, so the device resends it uncompressed.
            import zlib
            request(base, '/api/notify/PACKED?EV=1&TS=1000')
            text = ','.join(f'0:{20000+250*i},10C:{900+i},40C:0,10D:{i % 90},40D:0,89:1' for i in range(40))
            raw = (text + f'*{sum(text.encode()) & 255:X}').encode()
            packed = zlib.compress(raw, 6)
            assert request(base, '/api/post/PACKED?z=1', packed).strip() == b'OK 200'
            archive = ''.join(path.read_text() for path in (root / 'data' / 'PACKED').rglob('*.txt'))
            assert all(f'0:{20000+250*i},' in archive for i in range(40))
            try:
                request(base, '/api/post/PACKED?z=1', raw)
            except HTTPError as error:
                assert error.code == 400
            else:
                raise AssertionError('Uncompressed body accepted as compressed')
            print(f'PASS: zlib batch accepted ({len(raw)} -> {len(packed)} bytes), corrupt compressed body refused')
            # Lifecycle fields reach the Prometheus metrics the dashboard reads.
            text = '0:40000,89:1,97:1,98:2,99:1,8D:5000'
            assert b'OK' in request(base, '/api/post/PACKED', (text + f'*{sum(text.encode()) & 255:X}').encode())
            metrics = request(base, '/metrics').decode()
            for name, value in (('rejected_readings', '1'), ('power_phase', '2'), ('wake_reason', '1')):
                assert any(line.startswith(f'freematics_device_{name}{{device_id="PACKED"') and line.endswith(' ' + value)
                           for line in metrics.splitlines()), (name, [l for l in metrics.splitlines() if name in l])
            print('PASS: power phase, wake reason and rejected readings exported as metrics')
            # A Mode 01 value is repeated in every 250 ms sample. The paired
            # 0x400 age is the elapsed time since its ECU response, not a
            # second measurement. Prometheus must publish that acquisition age
            # plus the small server receipt delay. An old, mismatched 0x400
            # value must not make a new value look stale; legacy rows retain
            # the prior sample-timestamp calculation.
            request(base, '/api/notify/FRESHNESS?EV=1&TS=1000')
            text = '0:1000,10C:900,40C:10000,89:1'
            assert b'OK' in request(base, '/api/post/FRESHNESS', (text + f'*{sum(text.encode()) & 255:X}').encode())
            metrics = request(base, '/metrics').decode()
            held_age = metric_value(metrics, 'FRESHNESS', 0x10C)
            assert 10.0 <= held_age < 10.5, held_age
            # 1,500 ms must remain 1.5 seconds, rather than be treated as an
            # already-scaled seconds field.
            text = '0:1250,10C:900,40C:1500,89:1'
            assert b'OK' in request(base, '/api/post/FRESHNESS', (text + f'*{sum(text.encode()) & 255:X}').encode())
            metrics = request(base, '/metrics').decode()
            fractional_age = metric_value(metrics, 'FRESHNESS', 0x10C)
            assert 1.5 <= fractional_age < 2.0, fractional_age
            live = json.loads(request(base, '/api/get/FRESHNESS'))
            rpm = next(row for row in live['data'] if int(row[0]) == 0x10C)
            assert 1500 <= int(rpm[2]) < 2000, rpm
            channels = json.loads(request(base, '/api/channels/FRESHNESS?data=1'))
            rpm = next(row for row in channels['data'] if int(row[0]) == 0x10C)
            assert 1500 <= int(rpm[2]) < 2000, rpm
            # The 0x400 age is usable only in the sample that contains both
            # fields. Here the old 1,500 ms age does not apply to the new RPM.
            text = '0:1500,10C:900,89:1'
            assert b'OK' in request(base, '/api/post/FRESHNESS', (text + f'*{sum(text.encode()) & 255:X}').encode())
            metrics = request(base, '/metrics').decode()
            mismatched_age = metric_value(metrics, 'FRESHNESS', 0x10C)
            assert 0.0 <= mismatched_age < 0.5, mismatched_age
            # Legacy firmware emits no 0x400 field. It still reports age from
            # the last sample that contained the PID.
            request(base, '/api/notify/LEGACYAGE?EV=1&TS=1000')
            for text in ('0:1000,10C:900,89:1', '0:2250,89:1'):
                assert b'OK' in request(base, '/api/post/LEGACYAGE', (text + f'*{sum(text.encode()) & 255:X}').encode())
            metrics = request(base, '/metrics').decode()
            legacy_age = metric_value(metrics, 'LEGACYAGE', 0x10C)
            assert 1.25 <= legacy_age < 1.75, legacy_age
            channels = json.loads(request(base, '/api/channels/LEGACYAGE?data=1'))
            rpm = next(row for row in channels['data'] if int(row[0]) == 0x10C)
            assert 1250 <= int(rpm[2]) < 1750, rpm
            # A valid maximum uint32 age is a stale acquisition and must stay
            # stale across the device millisecond-clock rollover.
            text = '0:4294967000,10C:900,40C:4294967295,89:1,0:100,10C:900,40C:4294967295,89:1'
            assert b'OK' in request(base, '/api/post/FRESHNESS', (text + f'*{sum(text.encode()) & 255:X}').encode())
            metrics = request(base, '/metrics').decode()
            rollover_age = metric_value(metrics, 'FRESHNESS', 0x10C)
            assert 4294967.295 <= rollover_age < 4294967.8, rollover_age
            print('PASS: paired OBD acquisition ages export in milliseconds, preserve fractional seconds, reject mismatched ages and survive clock rollover')
            # A disconnected ECU must retain explicitly aged values while
            # legacy firmware without ages must still clear stale live data.
            text = '0:2000,10C:900,40C:1000,89:0'
            packet = (text + f'*{sum(text.encode()) & 255:X}').encode()
            assert b'OK' in request(base, '/api/post/FULLRATE', packet)
            live = json.loads(request(base, '/api/get/FULLRATE'))
            values = {int(row[0]): row[1] for row in live['data']}
            assert values.get(0x40C) == 1000, values
            assert values.get(0x10C) == 900, values
            files = list((root / 'data').rglob('*.txt'))
            archived = ''.join(p.read_text() for p in files)
            assert all(f'40C:{250*i}' in archived for i in range(4)), archived
            text = '0:2250,10C:900,89:0'
            packet = (text + f'*{sum(text.encode()) & 255:X}').encode()
            assert b'OK' in request(base, '/api/post/FULLRATE', packet)
            legacy_live = json.loads(request(base, '/api/get/FULLRATE'))
            assert not any(int(row[0]) == 0x10C for row in legacy_live['data']), legacy_live
            # Simulate a saved channel file from the previous four-range
            # binary by removing only the new range from each native record.
            server.terminate()
            try:
                server.wait(timeout=1)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait()
            state = root / 'data/channels.dat'
            current = state.read_bytes()
            stride = len(current) // 16
            prefix, extension = 48 + 1024 * 28, 256 * 28
            legacy = b''.join(current[i*stride:i*stride+prefix] + current[i*stride+prefix+extension:(i+1)*stride] for i in range(16))
            state.write_bytes(legacy)
            server = subprocess.Popen(command, cwd=root, stdout=log, stderr=log)
            for _ in range(120):
                try:
                    request(base, '/api/test'); break
                except OSError:
                    time.sleep(.05)
            request(base, '/api/get/FULLRATE') # retained channel identity without logging in again
            print('PASS: 250ms samples acknowledged, held RPM + age visible through live API, all ages retained in raw archive; aged disconnect values retained, legacy unaged disconnect values cleared, legacy channel identity survives restart')
        finally:
            server.terminate()
            try:
                server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
