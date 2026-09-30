#!/usr/bin/env python3
"""Exercise the real collector HTTP ingestion/API/archive with held readings."""
from pathlib import Path
import json
import socket
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def request(base, path, data=None):
    req = urllib.request.Request(base + path, data=data, headers={'Authorization': 'Bearer ' + 'A' * 64, 'Content-Type': 'application/octet-stream'})
    with urllib.request.urlopen(req, timeout=3) as response:
        return response.read()


with tempfile.TemporaryDirectory(prefix='freematics-collector-sampling-') as directory:
    root = Path(directory)
    (root / 'data').mkdir()
    (root / 'log').mkdir()
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    with (root / 'server.log').open('wb') as log:
        command = [str(ROOT / 'collector/teleserver'), '-g', '-p', str(port), '-u', '0', '-w', 'sampling-fixture-only',
                   '-d', str(root / 'data'), '-l', str(root / 'log')]
        server = subprocess.Popen(command, cwd=root, stdout=log, stderr=log)
        try:
            for _ in range(50):
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
            for _ in range(50):
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
