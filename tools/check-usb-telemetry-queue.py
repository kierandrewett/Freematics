#!/usr/bin/env python3
"""Exercise the production Model B USB queue's slow-reader policy."""

from __future__ import annotations

import pathlib
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="freematics-usb-queue-") as temp:
        work = pathlib.Path(temp)
        (work / "Arduino.h").write_text(
            "#pragma once\n#include <cstdint>\nusing byte = uint8_t;\n"
        )
        freertos = work / "freertos"
        freertos.mkdir()
        (freertos / "FreeRTOS.h").write_text("#pragma once\n")
        (freertos / "portmacro.h").write_text(
            "#pragma once\n#include <mutex>\nusing portMUX_TYPE = std::mutex;\n"
            "#define portMUX_INITIALIZER_UNLOCKED {}\n"
            "#define portENTER_CRITICAL(mux) (mux)->lock()\n"
            "#define portEXIT_CRITICAL(mux) (mux)->unlock()\n"
        )
        source = work / "queue_test.cpp"
        source.write_text(
            '#include "usbtelemetry.h"\n'
            "#include <cassert>\n"
            "#include <atomic>\n#include <chrono>\n#include <cstdio>\n"
            "#include <thread>\n"
            "static void put(UsbTelemetryQueue& queue, uint32_t value) {\n"
            "  auto* record = queue.reserve(); assert(record);\n"
            "  int length = std::snprintf(record->payload, sizeof(record->payload), \"%u:%08X\\n\", value, value ^ 0xA5A5A5A5u);\n"
            "  assert(length > 0); assert(queue.publish(record, (uint16_t)length));\n"
            "}\n"
            "static void verify(const UsbTelemetryRecord* record, uint32_t& previous) {\n"
            "  unsigned value = 0, check = 0;\n"
            "  assert(std::sscanf(record->payload, \"%u:%x\", &value, &check) == 2);\n"
            "  assert(check == (value ^ 0xA5A5A5A5u)); assert(value > previous); previous = value;\n"
            "}\n"
            "int main() {\n"
            "  UsbTelemetryQueue queue;\n"
            "  put(queue, 1); auto* active = queue.peek(); assert(active);\n"
            "  const char firstByte = active->payload[0];\n"
            "  put(queue, 2); put(queue, 3); put(queue, 4);\n"
            "  put(queue, 5);\n"
            "  assert(active->payload[0] == firstByte);\n"
            "  assert(queue.dropped() == 3);\n"
            "  uint32_t previous = 0; verify(active, previous);\n"
            "  queue.release();\n"
            "  auto* latest = queue.peek(); assert(latest);\n"
            "  verify(latest, previous); assert(previous == 5);\n"
            "  queue.release();\n"
            "  assert(queue.peek() == nullptr);\n"
            "  UsbTelemetryQueue stress; std::atomic<bool> done{false};\n"
            "  std::thread producer([&] { for (uint32_t n = 1; n <= 20000; ++n) { auto* r = stress.reserve(); if (!r) continue; int len = std::snprintf(r->payload, sizeof(r->payload), \"%u:%08X\\n\", n, n ^ 0xA5A5A5A5u); assert(len > 0); stress.publish(r, (uint16_t)len); } done = true; });\n"
            "  uint32_t consumed = 0;\n"
            "  while (!done.load()) { auto* r = stress.peek(); if (!r) { std::this_thread::yield(); continue; } verify(r, consumed); stress.release(); }\n"
            "  producer.join(); while (auto* r = stress.peek()) { verify(r, consumed); stress.release(); }\n"
            "}\n"
        )
        binary = work / "queue_test"
        subprocess.run(
            [
                "c++",
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(work),
                "-I",
                str(ROOT),
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        subprocess.run([str(binary)], check=True)
    print("PASS: mutex-protected producer/consumer queue preserves in-flight frames, drops stale backlog, and keeps emitted records valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
