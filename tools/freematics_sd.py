#!/usr/bin/env python3
"""Browse the Model B SD card through its USB serial connection."""

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

import serial


class PassiveSerial(serial.Serial):
    """Keep POSIX control lines unchanged on this board's reset circuit."""

    # pyserial 3.5 changes these lines separately during open(). The temporary
    # difference can reset the ESP32. This reader never controls boot/reset.
    def _update_dtr_state(self) -> None:
        pass

    def _update_rts_state(self) -> None:
        pass


class Device:
    def __init__(self, port: str, log: Path | None = None):
        self.lock = threading.Lock()
        self.sequence = secrets.randbits(31)
        self.log = log.open("ab", buffering=0) if log else None
        self.serial = PassiveSerial() if os.name == "posix" else serial.Serial()
        self.serial.port = port
        self.serial.baudrate = 115200
        self.serial.timeout = 0.3
        self.serial.write_timeout = 3
        self.serial.dtr = False
        self.serial.rts = False
        self.serial.open()
        # Do not reset the logger when the last host descriptor closes.
        if os.name == "posix":
            import termios
            attributes = termios.tcgetattr(self.serial.fileno())
            attributes[2] &= ~termios.HUPCL
            termios.tcsetattr(self.serial.fileno(), termios.TCSANOW, attributes)

    def request(self, operation: str, timeout: float = 35) -> dict:
        with self.lock:
            self.sequence += 1
            sequence = self.sequence
            command = f"FSD {sequence} {operation}\n".encode()
            pattern = re.compile(rb"\[SD " + str(sequence).encode() + rb"\] (\{[^\r\n]*\})\r?\n")
            deadline = time.monotonic() + timeout
            received = bytearray()
            last_send = 0.0
            while time.monotonic() < deadline:
                if time.monotonic() - last_send >= 4:
                    self.serial.write(command)
                    last_send = time.monotonic()
                data = self.serial.read(max(1, min(self.serial.in_waiting, 8192)))
                if data:
                    if self.log:
                        self.log.write(data)
                    received.extend(data)
                    match = pattern.search(received)
                    if match:
                        try:
                            result = json.loads(match[1])
                        except (UnicodeError, json.JSONDecodeError):
                            received.clear()
                            continue
                        if "error" in result:
                            raise OSError(errno.EIO, result["error"])
                        return result
                    if len(received) > 32768:
                        del received[:-16384]
            raise TimeoutError(f"No SD response to {operation.split()[0]}")

    def list(self, directory: str) -> list[dict]:
        entries = []
        offset = 0
        while True:
            result = self.request(f"LIST {directory} {offset}")
            entries.extend(result["files"])
            if not result["more"]:
                return entries
            if result["next"] <= offset:
                raise OSError(errno.EIO, "Invalid directory continuation")
            offset = result["next"]

    def read(self, path: str, size: int, offset: int) -> bytes:
        output = bytearray()
        while len(output) < size:
            position = offset + len(output)
            count = min(256, size - len(output))
            result = self.request(f"READ {path} {position} {count}")
            if result.get("offset") != position:
                raise OSError(errno.EIO, "SD response offset mismatch")
            data = bytes.fromhex(result["hex"])
            if len(data) > count:
                raise OSError(errno.EIO, "SD response length mismatch")
            output.extend(data)
            if len(data) < count:
                break
        return bytes(output)

    def close(self) -> None:
        self.serial.close()
        if self.log:
            self.log.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
    parser.add_argument("--log", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    listing = commands.add_parser("list")
    listing.add_argument("directory", nargs="?", default="/DATA")
    read = commands.add_parser("read")
    read.add_argument("path")
    read.add_argument("output", type=Path)
    commands.add_parser("beep")
    args = parser.parse_args()

    device = Device(args.port, args.log)
    try:
        device.request(f"TIME {int(time.time())}")
        if args.command == "status":
            print(json.dumps(device.request("STATUS"), indent=2))
        elif args.command == "list":
            print(json.dumps(device.list(args.directory), indent=2))
        elif args.command == "read":
            size = device.request(f"STAT {args.path}")["size"]
            with args.output.open("xb") as output:
                for offset in range(0, size, 4096):
                    output.write(device.read(args.path, min(4096, size - offset), offset))
        elif args.command == "beep":
            print(device.request("BEEP"))
    finally:
        device.close()


if __name__ == "__main__":
    main()
