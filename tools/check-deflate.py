#!/usr/bin/env python3
"""Check the firmware deflate encoder (teledeflate.cpp) against zlib.

Every input must come back byte for byte through Python's zlib, which is the
same inflate the collector uses. Telemetry-like batches also report the
compression ratio. Pass an archive file (collector .txt) to measure real data.
"""
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
#include <cstdio>
#include <cstdlib>
#include <vector>
#include "teledeflate.h"
int main(int argc, char** argv) {
    std::vector<unsigned char> input;
    int c;
    while ((c = getchar()) != EOF) input.push_back((unsigned char)c);
    const size_t capacity = argc > 1 ? strtoul(argv[1], nullptr, 10) : input.size() + input.size() / 8 + 64;
    std::vector<unsigned char> output(capacity);
    static DeflateScratch scratch;
    const size_t size = zlibCompress(input.data(), input.size(), output.data(), capacity, &scratch);
    fwrite(output.data(), 1, size, stdout);
    return size ? 0 : 3;
}
'''


def telemetry_batch(seed: int, samples: int) -> bytes:
    rng = random.Random(seed)
    rows = []
    for index in range(samples):
        fields = [f"0:{1000 + index * 250}"]
        for pid in range(0x100, 0x140):
            fields.append(f"{pid:X}:{rng.choice([0, 1, 12.5, 900 + index % 40, rng.randint(0, 255)])}")
            fields.append(f"{pid | 0x300:X}:{rng.randint(0, 3000)}")
        fields += [f"24:{1240 + index % 30}", f"81:-{70 + index % 9}", "89:1", f"8D:{9600000 - index * 900}"]
        rows.append(",".join(fields))
    return ",".join(rows).encode()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="freematics-deflate-") as directory:
        binary = Path(directory) / "deflate"
        source = Path(directory) / "harness.cpp"
        source.write_text(HARNESS)
        subprocess.run(["c++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-I", str(ROOT), str(source),
                        str(ROOT / "teledeflate.cpp"), "-o", str(binary)], check=True)

        def compress(data: bytes, capacity: int | None = None) -> subprocess.CompletedProcess:
            command = [str(binary)] + ([str(capacity)] if capacity else [])
            return subprocess.run(command, input=data, capture_output=True)

        rng = random.Random(20261001)
        cases = {
            "empty": b"",
            "one byte": b"x",
            "three bytes": b"abc",
            "long run": b"a" * 70000,
            "run then text": b"z" * 300 + b"0:1000,10C:900," * 400,
            "random 48 KB": bytes(rng.getrandbits(8) for _ in range(49152)),
            "distance 32768": bytes(rng.getrandbits(8) for _ in range(32768)) * 2,
        }
        for seed in range(20):
            cases[f"telemetry batch {seed}"] = telemetry_batch(seed, 40)
        # Huffman edge cases: tiny and skewed alphabets, one repeated symbol,
        # every byte value, and sizes up to the 64 KB collector limit.
        for seed in range(300):
            local = random.Random(seed)
            alphabet = bytes(local.sample(range(256), local.choice([1, 2, 3, 5, 12, 40, 256])))
            size = local.choice([0, 1, 2, 3, 4, 7, 100, 1000, 4096, 20000, 65000])
            weights = [local.random() ** local.choice([1, 4, 12]) for _ in alphabet]
            cases[f"random {seed}"] = bytes(local.choices(alphabet, weights, k=size))
        cases["all byte values"] = bytes(range(256)) * 50
        if len(sys.argv) > 1:
            text = Path(sys.argv[1]).read_text()
            samples = [s for line in text.splitlines() for s in line.split("*")[0].split(",0:")]
            samples = [s if s.startswith("0:") else "0:" + s for s in samples if s]
            for start in range(0, min(len(samples), 400), 40):
                cases[f"archive batch {start // 40}"] = ",".join(samples[start:start + 40]).encode()

        failures = 0
        raw_total = packed_total = zlib_total = 0
        for name, data in cases.items():
            result = compress(data)
            try:
                okay = result.returncode == 0 and zlib.decompress(result.stdout) == data
            except zlib.error as error:
                okay = False
                print(f"FAIL: {name}: {error}")
            if not okay:
                failures += 1
                print(f"FAIL: {name} ({len(data)} bytes)")
                continue
            if "batch" in name:
                raw_total += len(data)
                packed_total += len(result.stdout)
                zlib_total += len(zlib.compress(data, 6))
        # A full output buffer must report failure, never a truncated stream.
        tight = compress(cases["random 48 KB"], 1024)
        if tight.returncode != 3 or tight.stdout:
            failures += 1
            print("FAIL: output overflow was not reported")
        if failures:
            print(f"FAIL: {failures} case(s)")
            return 1
        print(f"PASS: {len(cases)} inputs round-trip through zlib; output overflow reported")
        print(f"PASS: batches {raw_total} -> {packed_total} bytes ({raw_total / packed_total:.1f}x); "
              f"zlib level 6 would give {raw_total / zlib_total:.1f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
