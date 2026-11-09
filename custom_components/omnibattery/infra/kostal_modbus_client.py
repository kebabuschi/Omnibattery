"""Async Modbus TCP client for KOSTAL PLENTICORE inverters.

KOSTAL exposes a configurable 32-bit word order at holding register 5.  The
PLENTICORE plus commonly uses CDAB (value 0), while some installations use
ABCD (value 1), so floats must not be decoded with a hard-coded word order.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import struct
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusIOException

_LOGGER = logging.getLogger(__name__)

_TIMEOUT_S = 10.0
_MESSAGE_WAIT_S = 0.02


class KostalModbusClient:
    """Serialized Modbus TCP transport with KOSTAL float helpers."""

    def __init__(self, host: str, port: int = 1502, slave_id: int = 71) -> None:
        self.host = host
        self.port = port
        self.unit_id = slave_id
        self._client: Optional[AsyncModbusTcpClient] = None
        self._slave_kwarg = "slave"
        self._lock = asyncio.Lock()
        self._word_swapped_32bit = True
        self._shutting_down = False

    @property
    def connected(self) -> bool:
        return bool(self._client is not None and getattr(self._client, "connected", False))

    def set_shutting_down(self, value: bool) -> None:
        self._shutting_down = bool(value)

    async def async_connect(self) -> bool:
        if self.connected:
            return True
        await self.async_close()
        client = AsyncModbusTcpClient(self.host, port=self.port, timeout=_TIMEOUT_S)
        try:
            if not await client.connect():
                return False
            self._client = client
            self._slave_kwarg = self._detect_slave_kwarg(client)
            # Register 5 itself is U16 and therefore independent of 32-bit word order.
            order = await self._read_words_unlocked(5, 1)
            if order:
                self._word_swapped_32bit = (order[0] & 0xFF) == 0
            return True
        except Exception as err:
            if not self._shutting_down:
                _LOGGER.warning("KOSTAL Modbus connect failed %s:%s: %s", self.host, self.port, err)
            await self.async_close()
            return False

    async def async_close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                result = client.close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

    @staticmethod
    def _detect_slave_kwarg(client) -> str:
        try:
            params = inspect.signature(client.read_holding_registers).parameters
            for name in ("slave", "unit", "dev_id", "device_id"):
                if name in params:
                    return name
        except Exception:
            pass
        return "slave"

    async def _read_words_unlocked(self, address: int, count: int) -> Optional[list[int]]:
        if self._client is None or not self.connected:
            return None
        kwargs = {self._slave_kwarg: self.unit_id}
        try:
            result = await asyncio.wait_for(
                self._client.read_holding_registers(address=address, count=count, **kwargs),
                timeout=_TIMEOUT_S,
            )
        except (ConnectionException, ModbusIOException, asyncio.TimeoutError):
            return None
        except Exception as err:
            if not self._shutting_down:
                _LOGGER.debug("KOSTAL read %s failed: %s", address, err)
            return None
        if result.isError():
            return None
        regs = getattr(result, "registers", None)
        return list(regs[:count]) if regs is not None and len(regs) >= count else None

    async def read_words(self, address: int, count: int) -> Optional[list[int]]:
        async with self._lock:
            if not self.connected and not await self.async_connect():
                return None
            try:
                return await self._read_words_unlocked(address, count)
            finally:
                if not self._shutting_down:
                    await asyncio.sleep(_MESSAGE_WAIT_S)

    def _words_to_float(self, words: list[int]) -> Optional[float]:
        if len(words) < 2:
            return None
        first, second = words[0] & 0xFFFF, words[1] & 0xFFFF
        raw = struct.pack(">HH", second, first) if self._word_swapped_32bit else struct.pack(">HH", first, second)
        value = struct.unpack(">f", raw)[0]
        return value if math.isfinite(value) else None

    async def read_float(self, address: int) -> Optional[float]:
        words = await self.read_words(address, 2)
        return self._words_to_float(words) if words else None

    async def read_i16(self, address: int) -> Optional[int]:
        words = await self.read_words(address, 1)
        if not words:
            return None
        value = words[0] & 0xFFFF
        return value - 0x10000 if value >= 0x8000 else value

    async def read_u16(self, address: int) -> Optional[int]:
        words = await self.read_words(address, 1)
        return (words[0] & 0xFFFF) if words else None


    async def read_u32(self, address: int) -> Optional[int]:
        """Read KOSTAL battery identity U32 values in documented big-endian order."""
        words = await self.read_words(address, 2)
        if not words:
            return None
        return ((words[0] & 0xFFFF) << 16) | (words[1] & 0xFFFF)
