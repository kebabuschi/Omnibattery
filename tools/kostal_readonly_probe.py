#!/usr/bin/env python3
"""Read-only KOSTAL PLENTICORE Modbus probe.

This utility performs holding-register reads only. It deliberately contains no
write-register calls and is independent of Home Assistant / Omnibattery control.
Requires pymodbus >= 3.8.0.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import math
import struct

from pymodbus.client import AsyncModbusTcpClient


def decode_float(words: list[int], swapped: bool) -> float | None:
    if len(words) != 2:
        return None
    a, b = (words[1], words[0]) if swapped else (words[0], words[1])
    value = struct.unpack(">f", struct.pack(">HH", a, b))[0]
    return value if math.isfinite(value) else None


def decode_string(words: list[int]) -> str | None:
    raw = b"".join(int(word).to_bytes(2, "big") for word in words)
    value = raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()
    return value or None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="PLENTICORE IP address or hostname")
    parser.add_argument("--port", type=int, default=1502)
    parser.add_argument("--unit", type=int, default=71)
    args = parser.parse_args()

    client = AsyncModbusTcpClient(args.host, port=args.port, timeout=8)
    if not await client.connect():
        print(f"Connection failed: {args.host}:{args.port}")
        return 2

    method = client.read_holding_registers
    params = inspect.signature(method).parameters
    unit_key = next((key for key in ("slave", "unit", "dev_id", "device_id") if key in params), "slave")

    async def read(address: int, count: int = 1) -> list[int] | None:
        response = await method(address=address, count=count, **{unit_key: args.unit})
        if response.isError():
            return None
        values = getattr(response, "registers", None)
        return list(values[:count]) if values is not None and len(values) >= count else None

    try:
        order = await read(5)
        if not order:
            print("Could not read register 5 (32-bit word order); aborting.")
            return 3
        swapped = (order[0] & 0xFF) == 0
        print(f"32-bit word order register 5: 0x{order[0]:04X} ({'CDAB' if swapped else 'ABCD'})")

        specs = [
            ("Battery SoC", 210, 2, "float"),
            ("Battery temperature", 214, 2, "float"),
            ("Battery voltage", 216, 2, "float"),
            ("Battery power (KOSTAL sign)", 582, 1, "i16"),
            ("Battery model ID", 525, 2, "u32"),
            ("Battery serial", 527, 2, "u32"),
            ("Inverter model", 768, 32, "string"),
            ("Inverter serial", 559, 16, "string"),
        ]
        for label, address, count, kind in specs:
            words = await read(address, count)
            if words is None:
                print(f"{label}: read failed (register {address})")
                continue
            if kind == "float":
                value = decode_float(words, swapped)
            elif kind == "i16":
                raw = words[0] & 0xFFFF
                value = raw - 0x10000 if raw >= 0x8000 else raw
            elif kind == "u32":
                value = ((words[0] & 0xFFFF) << 16) | (words[1] & 0xFFFF)
            else:
                value = decode_string(words)
            print(f"{label} [reg {address}]: {value!r} (raw={words})")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
