"""Unit tests for the read-only KOSTAL PLENTICORE prototype.

All Modbus responses are simulated. These tests never open a socket or touch an
inverter; they pin word-order decoding, signed power normalization, identity
register handling, and the absence of write behavior.
"""
from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery.drivers.kostal import (
    KostalPlenticoreDriver,
)
from custom_components.omnibattery.infra.kostal_modbus_client import (
    KostalModbusClient,
)


def _words(value: float, swapped: bool) -> list[int]:
    first, second = struct.unpack(">HH", struct.pack(">f", value))
    return [second, first] if swapped else [first, second]


@pytest.mark.parametrize("swapped", [False, True])
@pytest.mark.parametrize("value", [0.0, 42.5, -12.25, 230.0])
def test_float_decoder_supports_both_word_orders(swapped, value):
    client = KostalModbusClient("unused")
    client._word_swapped_32bit = swapped
    assert client._words_to_float(_words(value, swapped)) == pytest.approx(value)


def test_float_decoder_rejects_nan_and_infinity():
    client = KostalModbusClient("unused")
    client._word_swapped_32bit = False
    for value in (float("nan"), float("inf"), float("-inf")):
        a, b = struct.unpack(">HH", struct.pack(">f", value))
        assert client._words_to_float([a, b]) is None


@pytest.mark.parametrize("raw,expected", [(0, 0), (1, 1), (0x7FFF, 32767), (0x8000, -32768), (0xFFFF, -1)])
def test_signed_16_bit_conversion(raw, expected):
    client = KostalModbusClient("unused")
    client.read_words = AsyncMock(return_value=[raw])
    assert asyncio.run(client.read_i16(582)) == expected


def test_unsigned_32_bit_identity_conversion():
    # The BYD identity register pair is decoded as an unsigned 32-bit integer.
    client = KostalModbusClient("unused")
    client.read_words = AsyncMock(return_value=[0x1234, 0xABCD])
    assert asyncio.run(client.read_u32(527)) == 0x1234ABCD


class FakeKostalClient:
    def __init__(self):
        self.connected = True
        self.read_float_values = {210: 73.5, 214: 24.25, 216: 512.0}
        self.read_words_values = {768: [0x504C, 0x454E, 0x5449, 0x434F, 0x5245, 0], 559: [0x494E, 0x5600]}
        self.read_u32_values = {525: 1234, 527: 987654321}

    async def read_float(self, address):
        return self.read_float_values.get(address)

    async def read_i16(self, address):
        return 1250

    async def read_words(self, address, count):
        return self.read_words_values.get(address)

    async def read_u32(self, address):
        return self.read_u32_values.get(address)


def _driver_with_fake_client():
    client = FakeKostalClient()
    return client, KostalPlenticoreDriver('unused', client=client)

def test_telemetry_normalizes_kostal_discharge_sign_and_reads_byd_identity():
    client, driver = _driver_with_fake_client()
    data = asyncio.run(driver.read_telemetry())
    assert data["battery_soc"] == 73.5
    # KOSTAL positive = discharge; Omnibattery positive = charge.
    assert data["battery_power"] == -1250
    assert data["temperature"] == 24.25
    assert data["battery_voltage"] == 512.0
    assert data["battery_model_id"] == 1234
    assert data["battery_serial"] == 987654321
    assert client.read_u32_values[527] == 987654321


def test_apply_setpoint_refuses_commands_in_read_only_phase():
    client, driver = _driver_with_fake_client()
    result = asyncio.run(driver.apply_setpoint(500))
    assert result.ok is False
    assert result.failure_reason == "read_only_test_driver"
    assert not hasattr(client, "write_float")
    assert not hasattr(client, "write_registers")


def test_standby_is_a_noop_in_read_only_phase():
    client, driver = _driver_with_fake_client()
    # A no-op must not be reported as a successfully executed standby command.
    assert asyncio.run(driver.standby()) is False
    assert not hasattr(client, "write_float")
    assert not hasattr(client, "write_registers")


def test_read_only_driver_has_expected_identity_and_limits():
    _, driver = _driver_with_fake_client()
    assert driver.dc_coupled is True
    assert driver.capabilities.max_charge_power_w == 2800
    assert driver.capabilities.max_discharge_power_w == 2800

    assert driver.read_only is True
    assert driver.capabilities.hardware_soc_cutoff is False
    assert driver.net_power_from_data({"commanded_net_power": 500}) is None
    assert asyncio.run(driver.apply_config(max_soc_pct=100)) is False


def test_zero_battery_identity_registers_are_omitted():
    client, driver = _driver_with_fake_client()
    client.read_u32_values = {525: 0, 527: 0}
    data = asyncio.run(driver.read_telemetry())
    assert "battery_model_id" not in data
    assert "battery_serial" not in data
