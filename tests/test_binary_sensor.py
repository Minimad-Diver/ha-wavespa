"""Tests for binary_sensor.py.

Only the connectivity sensor remains. The Errors sensor was removed in #50:
the fault attributes it matched are Bestway heritage and a real Wave_SPA_EU
reports none of them, so it could never turn on.
"""

from collections.abc import Iterable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from homeassistant.helpers.entity import Entity

from custom_components.wavespa.binary_sensor import (
    DeviceConnectivitySensor,
    async_setup_entry,
)
from custom_components.wavespa.wavespa.api import WavespaApiResults
from custom_components.wavespa.wavespa.model import WavespaDevice, WavespaDeviceStatus


def _make_device(
    is_online: bool = True, product_name: str = "Wave_SPA_EU"
) -> WavespaDevice:
    return WavespaDevice(
        protocol_version=2,
        device_id="test_device",
        product_name=product_name,
        alias="Test Spa",
        mcu_soft_version="1.0",
        mcu_hard_version="1.0",
        wifi_soft_version="1.0",
        wifi_hard_version="1.0",
        is_online=is_online,
    )


def _make_coordinator(device: WavespaDevice, attrs: dict[str, Any] | None):
    coordinator = MagicMock()
    coordinator.api = MagicMock()
    coordinator.api.devices = {"test_device": device}
    devices = (
        {"test_device": WavespaDeviceStatus(timestamp=1000, attrs=dict(attrs))}
        if attrs is not None
        else {}
    )
    coordinator.data = WavespaApiResults(devices=devices)
    coordinator.last_update_success = True
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


class TestConnectivitySensor:
    """Reports the API's is_online flag, and stays available regardless."""

    def _make(self, is_online: bool) -> DeviceConnectivitySensor:
        from custom_components.wavespa.binary_sensor import (
            _SPA_CONNECTIVITY_SENSOR_DESCRIPTION,
        )

        coordinator = _make_coordinator(_make_device(is_online=is_online), {})
        return DeviceConnectivitySensor(
            coordinator,
            MagicMock(),
            "test_device",
            _SPA_CONNECTIVITY_SENSOR_DESCRIPTION,
        )

    def test_online(self) -> None:
        assert self._make(is_online=True).is_on is True

    def test_offline(self) -> None:
        assert self._make(is_online=False).is_on is False

    def test_available_even_when_offline(self) -> None:
        """The sensor must still report while the device claims to be offline."""
        assert self._make(is_online=False).available is True

    def test_unavailable_when_the_coordinator_fails(self) -> None:
        sensor = self._make(is_online=True)
        sensor.coordinator.last_update_success = False
        assert sensor.available is False


class TestSetupEntry:
    """Only supported spa types get binary sensors."""

    async def _setup(self, product_name: str) -> list[Any]:
        coordinator = _make_coordinator(_make_device(product_name=product_name), {})
        config_entry = MagicMock()
        config_entry.runtime_data = coordinator

        added: list[Any] = []

        def add_entities(
            new_entities: Iterable[Entity],
            update_before_add: bool = False,
            *,
            config_subentry_id: str | None = None,
        ) -> None:
            added.extend(new_entities)

        await async_setup_entry(MagicMock(), config_entry, add_entities)
        return added

    async def test_supported_spa_gets_the_connectivity_sensor(self) -> None:
        entities = await self._setup("Wave_SPA_EU")
        assert len(entities) == 1
        assert isinstance(entities[0], DeviceConnectivitySensor)

    async def test_us_spa_gets_the_connectivity_sensor(self) -> None:
        entities = await self._setup("Wave_SPA_US")
        assert len(entities) == 1

    async def test_unknown_device_gets_none(self) -> None:
        entities = await self._setup("Something_Else")
        assert entities == []
