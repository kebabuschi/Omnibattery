"""Read-only KOSTAL PLENTICORE telemetry prototype.

This prototype is intentionally not registered in Omnibattery's live coordinator
or config flow. It only reads Modbus telemetry while register decoding is validated.
The official Home Assistant KOSTAL integration can remain installed unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Optional

from .base import BatteryDriver, DriverCapabilities, ReadGroup, SetpointResult, TelemetrySnapshot
from ..infra.kostal_modbus_client import KostalModbusClient

_LOGGER = logging.getLogger(__name__)

REG_MODEL = 768
REG_INVERTER_SERIAL = 559
REG_BATTERY_MODEL_ID = 525
REG_BATTERY_SERIAL = 527
REG_SOC = 210
REG_TEMPERATURE = 214
REG_VOLTAGE = 216
REG_BATTERY_POWER = 582

_READ_GROUPS = [
    ReadGroup(scan_interval="high", keys=("battery_soc", "battery_power")),
    ReadGroup(scan_interval="low", keys=("battery_voltage", "temperature")),
]

_CAPS_DEFAULT = DriverCapabilities(
    # No KOSTAL write register has been validated or implemented yet.
    hardware_soc_cutoff=False,
    has_force_mode=False,
    push_telemetry=False,
    max_charge_power_w=2800,
    max_discharge_power_w=2800,
    has_mppt_pv=False,
    has_alarm_registers=False,
    has_rs485_control=False,
    has_energy_counters=False,
    has_nominal_capacity=False,
    has_daily_energy_counters=False,
    setpoint_confirm_reliable=False,
    actuator_latency_s=2.0,
    readback_latency_s=2.0,
    charge_cutoff_range=(50.0, 100.0),
    discharge_cutoff_range=(0.0, 30.0),
)


def _decode_string(words: list[int]) -> Optional[str]:
    raw = bytearray()
    for word in words:
        raw.extend(((word >> 8) & 0xFF, word & 0xFF))
    text = raw.split(b"\x00")[0].decode("ascii", "replace").strip()
    return text or None


class KostalPlenticoreDriver(BatteryDriver):
    """Native Modbus driver for a KOSTAL PLENTICORE hybrid battery."""

    def __init__(
        self,
        host: str,
        port: int = 1502,
        slave_id: int = 71,
        *,
        max_charge_power_w: int = 2800,
        max_discharge_power_w: int = 2800,
        client: Optional[KostalModbusClient] = None,
    ) -> None:
        self._client = client or KostalModbusClient(host, port, slave_id)
        self._shutting_down = False
        self._model: Optional[str] = None
        self._serial: Optional[str] = None
        self._last_net_power_w: Optional[int] = None
        self._capabilities = replace(
            _CAPS_DEFAULT,
            max_charge_power_w=max(100, int(max_charge_power_w)),
            max_discharge_power_w=max(100, int(max_discharge_power_w)),
        )

    @property
    def capabilities(self) -> DriverCapabilities:
        return self._capabilities

    @property
    def read_only(self) -> bool:
        """This prototype must never be used as a controllable battery."""
        return True

    @property
    def dc_coupled(self) -> bool:
        return True

    @property
    def model_label(self) -> Optional[str]:
        return self._model or "KOSTAL PLENTICORE"

    @property
    def serial(self) -> Optional[str]:
        return self._serial

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def read_groups(self) -> list[ReadGroup]:
        return _READ_GROUPS

    @property
    def sensor_definitions(self) -> list[dict]:
        return [
            {"key": "battery_soc", "name": "Battery SOC", "scan_interval": "high"},
            {"key": "battery_power", "name": "Battery Power", "scan_interval": "high"},
            {"key": "battery_voltage", "name": "Battery Voltage", "scan_interval": "low"},
            {"key": "temperature", "name": "Battery Temperature", "scan_interval": "low"},
        ]

    @property
    def number_definitions(self) -> list[dict]:
        return []

    @property
    def select_definitions(self) -> list[dict]:
        return []

    @property
    def switch_definitions(self) -> list[dict]:
        return []

    @property
    def binary_sensor_definitions(self) -> list[dict]:
        return []

    @property
    def button_definitions(self) -> list[dict]:
        return []

    @property
    def all_definitions(self) -> list[dict]:
        return self.sensor_definitions

    async def connect(self) -> bool:
        if not await self._client.async_connect():
            return False
        identity = await self.read_telemetry(["device_model", "battery_model_id", "battery_serial"])
        self._model = identity.get("device_model") or self._model
        battery_serial = identity.get("battery_serial")
        # Some PLENTICORE firmware returns zero for unavailable battery identity
        # registers; never publish that as a real serial.
        self._serial = str(battery_serial) if battery_serial not in (None, 0) else self._serial
        return True

    async def close(self) -> None:
        await self._client.async_close()

    def set_shutting_down(self, value: bool) -> None:
        self._shutting_down = bool(value)
        self._client.set_shutting_down(value)

    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        wanted = set(keys or (
            "battery_soc", "battery_power", "battery_voltage", "temperature",
            "device_model", "inverter_serial", "battery_model_id", "battery_serial",
        ))
        snapshot: TelemetrySnapshot = {}
        if "battery_soc" in wanted:
            value = await self._client.read_float(REG_SOC)
            if value is not None:
                snapshot["battery_soc"] = value
        if "battery_power" in wanted:
            value = await self._client.read_i16(REG_BATTERY_POWER)
            if value is not None:
                # KOSTAL: negative=charge, positive=discharge.
                snapshot["battery_power"] = -value
        if "battery_voltage" in wanted:
            value = await self._client.read_float(REG_VOLTAGE)
            if value is not None:
                snapshot["battery_voltage"] = value
        if "temperature" in wanted:
            value = await self._client.read_float(REG_TEMPERATURE)
            if value is not None:
                snapshot["temperature"] = value
        if "device_model" in wanted:
            words = await self._client.read_words(REG_MODEL, 32)
            if words:
                snapshot["device_model"] = _decode_string(words)
        if "inverter_serial" in wanted:
            words = await self._client.read_words(REG_INVERTER_SERIAL, 16)
            if words:
                snapshot["inverter_serial"] = _decode_string(words)
        if "battery_model_id" in wanted:
            value = await self._client.read_u32(REG_BATTERY_MODEL_ID)
            if value not in (None, 0):
                snapshot["battery_model_id"] = value
        if "battery_serial" in wanted:
            value = await self._client.read_u32(REG_BATTERY_SERIAL)
            if value not in (None, 0):
                snapshot["battery_serial"] = value
        return {key: value for key, value in snapshot.items() if value is not None}

    async def apply_setpoint(
        self, net_power_w: int, *, mode_hint: Optional[str] = None, read_back: bool = True
    ) -> SetpointResult:
        """Refuse all control commands during the read-only development phase."""
        return SetpointResult(
            False,
            int(net_power_w),
            False,
            failure_reason="read_only_test_driver",
        )

    async def write_control(self, key: str, value: int) -> bool:
        return False

    def net_power_from_data(self, data: dict) -> Optional[int]:
        # There is no accepted hardware setpoint in read-only mode. Returning a
        # cached/commanded value could make the controller incorrectly skip a
        # future write when control support is eventually implemented.
        return None

    @property
    def control_dependency_keys(self) -> frozenset:
        return frozenset({"battery_soc", "battery_power", "commanded_net_power"})

    async def apply_config(self, **_kwargs) -> bool:
        """Configuration writes are unsupported in the read-only prototype."""
        return False

    async def set_charge_cutoff(self, soc_pct: float) -> bool:
        return False

    async def standby(self) -> bool:
        """Report unsupported: read-only mode never writes a zero setpoint."""
        return False

    async def set_rs485_control(self, enable: bool) -> bool:
        return False

    async def get_rs485_control(self) -> Optional[bool]:
        return None

    @classmethod
    async def probe(
        cls, host: str, port: int = 1502, slave_id: int = 71
    ) -> tuple[bool, Optional[str], Optional[str]]:
        driver = cls(host, port, slave_id)
        try:
            if not await driver.connect():
                return False, None, None
            data = await driver.read_telemetry(["device_model", "battery_soc", "battery_model_id", "battery_serial"])
            model = data.get("device_model")
            return bool(model or "battery_soc" in data), model, driver.serial
        finally:
            await driver.close()
