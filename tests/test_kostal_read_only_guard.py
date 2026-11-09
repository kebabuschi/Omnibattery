"""Coordinator-level write barriers for explicitly read-only drivers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)


def _coordinator():
    driver = SimpleNamespace(
        read_only=True,
        write_control=AsyncMock(return_value=True),
        apply_setpoint=AsyncMock(),
        set_rs485_control=AsyncMock(return_value=True),
        apply_config=AsyncMock(return_value=True),
        set_charge_cutoff=AsyncMock(return_value=True),
        standby=AsyncMock(return_value=True),
    )
    coordinator = object.__new__(MarstekVenusDataUpdateCoordinator)
    coordinator.driver = driver
    return coordinator, driver


def test_all_coordinator_write_entrypoints_stop_before_driver():
    coordinator, driver = _coordinator()

    async def exercise():
        assert await coordinator.write_control("force_mode", 1) is False
        result = await coordinator.apply_power(500)
        assert result.ok is False
        assert result.failure_reason == "read_only_driver"
        assert await coordinator.set_rs485_control(True) is False
        assert await coordinator.apply_config(
            max_soc_pct=100,
            min_soc_pct=10,
            max_charge_power_w=1000,
            max_discharge_power_w=1000,
        ) is False
        assert await coordinator.set_charge_cutoff(100) is False
        assert await coordinator.standby() is False

    asyncio.run(exercise())

    driver.write_control.assert_not_awaited()
    driver.apply_setpoint.assert_not_awaited()
    driver.set_rs485_control.assert_not_awaited()
    driver.apply_config.assert_not_awaited()
    driver.set_charge_cutoff.assert_not_awaited()
    driver.standby.assert_not_awaited()
