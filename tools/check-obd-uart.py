#!/usr/bin/env python3
"""Exercise the production UART receive function at its read boundaries.

Failure cases:
1. The co-processor prompt ``\r>`` is one UART read.
2. The ``\r`` and ``>`` bytes are separate reads.
3. A response has no prompt before its timeout.
4. A full caller buffer does not write beyond its terminator.

The second case used to miss the prompt because the production code searched
only from the start of the latest UART read.  This harness extracts and
compiles the production function.  It also compiles the prior completion
check, so the regression case proves the old timeout behaviour.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "lib/FreematicsPlus/FreematicsPlus.cpp").read_text()


def extract(signature: str) -> str:
    start = SOURCE.index(signature + "\n{")
    opening = SOURCE.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (SOURCE[end] == "{") - (SOURCE[end] == "}")
        end += 1
    return SOURCE[start:end]


production = extract("int CLink_UART::receive(char* buffer, int bufsize, unsigned int timeout)")
expected = 'const char* prompt = strstr(buffer, "\\r>");'
if expected not in production:
    raise SystemExit("FAIL: UART completion check changed; update this harness deliberately")

legacy = production.replace(
    expected,
    'const char* prompt = strstr(buffer + n, "\\r>");',
).replace(
    "n = prompt - buffer + 1;",
    "n = n + len - 1;",
)
if legacy == production:
    raise SystemExit("FAIL: could not reconstruct the prior UART completion check")

code = r'''
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

#define VERBOSE_LINK 0
#define LINK_UART_NUM 2
#define OBD_TIMEOUT_LONG 100

struct CLink_UART { int receive(char* buffer, int bufsize, unsigned int timeout); };

struct Chunk { std::string bytes; };
static std::vector<Chunk> chunks;
static size_t nextChunk = 0;
static unsigned long now = 0;

unsigned long millis() { return now; }
int uart_read_bytes(int, uint8_t* destination, int capacity, int)
{
    now++;
    if (nextChunk == chunks.size()) return 0;
    const std::string& source = chunks[nextChunk++].bytes;
    const int size = std::min(capacity, static_cast<int>(source.size()));
    std::memcpy(destination, source.data(), size);
    if (size != static_cast<int>(source.size())) {
        chunks.insert(chunks.begin() + nextChunk, {{source.substr(size)}});
    }
    return size;
}

static void setChunks(std::initializer_list<const char*> input)
{
    chunks.clear();
    for (const char* item : input) chunks.push_back({item});
    nextChunk = 0;
    now = 0;
}

static int runProduction(std::initializer_list<const char*> input, char* buffer, int capacity, unsigned timeout)
{
    setChunks(input);
    CLink_UART link;
    return link.receive(buffer, capacity, timeout);
}
static int runLegacy(std::initializer_list<const char*> input, char* buffer, int capacity, unsigned timeout)
{
    setChunks(input);
    LegacyLink link;
    return link.receive(buffer, capacity, timeout);
}
'''
code += production + "\n"
# Compile the exact old body as a separate class.  The fake UART source is shared.
legacy_body = legacy.replace("CLink_UART::receive", "LegacyLink::receive")
code = code.replace(
    "struct CLink_UART { int receive(char* buffer, int bufsize, unsigned int timeout); };",
    "struct CLink_UART { int receive(char* buffer, int bufsize, unsigned int timeout); };\n"
    "struct LegacyLink { int receive(char* buffer, int bufsize, unsigned int timeout); };",
)
code += legacy_body + r'''

int main()
{
    char buffer[64] = {};

    int received = runProduction({"41 0C 0C 80\r>"}, buffer, sizeof(buffer), 10);
    assert(received == 12);
    assert(std::string(buffer) == "41 0C 0C 80\r");
    assert(now == 1);

    received = runProduction({"41 0C 0C 80\r", ">"}, buffer, sizeof(buffer), 10);
    assert(received == 12);
    assert(std::string(buffer) == "41 0C 0C 80\r");
    assert(now == 2);

    received = runLegacy({"41 0C 0C 80\r>"}, buffer, sizeof(buffer), 10);
    assert(received == 12);
    assert(std::string(buffer) == "41 0C 0C 80\r");
    assert(now == 1);

    received = runLegacy({"41 0C 0C 80\r", ">"}, buffer, sizeof(buffer), 10);
    assert(received == 13);
    assert(std::string(buffer) == "41 0C 0C 80\r>");
    assert(now > 10);

    received = runProduction({"NO DATA\r"}, buffer, sizeof(buffer), 4);
    assert(received == 8);
    assert(std::string(buffer) == "NO DATA\r");
    assert(now > 4);

    char limited[6] = {};
    received = runProduction({"123456789\r>"}, limited, sizeof(limited), 4);
    assert(received == 5);
    assert(std::string(limited) == "12345");

    std::cout << "PASS: UART response completion handles a split prompt, normal prompt, timeout and buffer limit\n";
}
'''

parser = argparse.ArgumentParser()
parser.add_argument("--report", type=Path, help="write JSON evidence")
arguments = parser.parse_args()

with tempfile.TemporaryDirectory(prefix="freematics-obd-uart-") as directory:
    directory = Path(directory)
    cpp = directory / "uart.cpp"
    executable = directory / "uart"
    cpp.write_text(code)
    subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", str(cpp), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)

if arguments.report:
    arguments.report.write_text(json.dumps({
        "check": "obd_uart_receive",
        "source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
        "result": "pass",
        "cases": ["single_prompt", "split_prompt", "legacy_single_prompt", "legacy_split_timeout", "timeout", "buffer_limit"],
    }, indent=2) + "\n")
