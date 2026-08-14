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

    async def test_supported_spa_gets_both_sensors(self) -> None:
        entities = await self._setup("Wave_SPA_EU")
        from custom_components.wavespa.binary_sensor import DeviceAlertsSensor

        assert any(isinstance(e, DeviceConnectivitySensor) for e in entities)
        assert any(isinstance(e, DeviceAlertsSensor) for e in entities)

    async def test_us_spa_gets_both_sensors(self) -> None:
        entities = await self._setup("Wave_SPA_US")
        assert len(entities) == 2

    async def test_unknown_device_gets_none(self) -> None:
        entities = await self._setup("Something_Else")
        assert entities == []


def _make_alerts_sensor(attrs: dict[str, Any] | None):
    from custom_components.wavespa.binary_sensor import (
        _SPA_ALERTS_SENSOR_DESCRIPTION,
        DeviceAlertsSensor,
    )

    coordinator = _make_coordinator(_make_device(), attrs)
    return DeviceAlertsSensor(
        coordinator, MagicMock(), "test_device", _SPA_ALERTS_SENSOR_DESCRIPTION
    )


class TestAlertsSensor:
    """Driven by the three datapoints the manufacturer types as "alert".

    Its predecessor searched for Bestway names (system_err*, E##, earth,
    error) that this hardware never sends, so it could never turn on. These
    three come from the product definition rather than a guess.
    """

    def test_clear_spa_reports_no_problem(self) -> None:
        sensor = _make_alerts_sensor(
            {"Overtime_filter": 0, "Superheat": 0, "Undercooling": 0}
        )
        assert sensor.is_on is False

    def test_superheat_is_a_problem(self) -> None:
        sensor = _make_alerts_sensor(
            {"Overtime_filter": 0, "Superheat": 1, "Undercooling": 0}
        )
        assert sensor.is_on is True

    def test_undercooling_is_a_problem(self) -> None:
        sensor = _make_alerts_sensor({"Undercooling": 1})
        assert sensor.is_on is True

    def test_overtime_filter_is_a_problem(self) -> None:
        sensor = _make_alerts_sensor({"Overtime_filter": 1})
        assert sensor.is_on is True

    def test_each_alert_is_reported_individually(self) -> None:
        """So an automation can act on the specific fault."""
        sensor = _make_alerts_sensor(
            {"Overtime_filter": 1, "Superheat": 0, "Undercooling": 0}
        )
        assert sensor.extra_state_attributes == {
            "Overtime_filter": True,
            "Superheat": False,
            "Undercooling": False,
        }

    def test_ordinary_attributes_are_ignored(self) -> None:
        """A running heater is not a fault."""
        sensor = _make_alerts_sensor(
            {"Heater": 1, "Filter": 1, "Bubble": 1, "Temperature_setup": 40}
        )
        assert sensor.is_on is False
        assert sensor.extra_state_attributes == {}

    def test_attributes_the_spa_does_not_send_are_omitted(self) -> None:
        """Reported clear would claim knowledge of a flag we never received."""
        sensor = _make_alerts_sensor({"Superheat": 0})
        assert sensor.extra_state_attributes == {"Superheat": False}

    def test_string_zero_is_not_a_problem(self) -> None:
        """bool("0") is True, so this needs the shared numeric coercion."""
        sensor = _make_alerts_sensor(
            {"Overtime_filter": "0", "Superheat": "0", "Undercooling": "0"}
        )
        assert sensor.is_on is False

    def test_string_one_is_a_problem(self) -> None:
        sensor = _make_alerts_sensor({"Superheat": "1"})
        assert sensor.is_on is True

    def test_unreadable_value_is_not_a_problem(self) -> None:
        sensor = _make_alerts_sensor({"Superheat": "unknown"})
        assert sensor.is_on is False

    def test_no_status_reports_no_problem(self) -> None:
        sensor = _make_alerts_sensor(None)
        assert sensor.is_on is False
        assert sensor.extra_state_attributes == {}

    def test_is_not_diagnostic(self) -> None:
        """A fault indicator belongs on the main card, not under Diagnostics."""
        assert _make_alerts_sensor({}).entity_category is None

    def test_unique_id_is_reused_from_the_removed_sensor(self) -> None:
        """Installs upgrading straight from 2.0.0 keep their entity."""
        sensor = _make_alerts_sensor({})
        assert sensor._attr_unique_id == "test_device_spa_has_error"


class TestObsoleteListDoesNotDeleteTheAlertsSensor:
    """The alerts sensor reuses a unique ID that setup used to prune.

    Left in the obsolete list, setup would delete the entity moments before
    the platform recreated it - on every restart, losing history each time.
    """

    def test_spa_has_error_is_not_pruned(self) -> None:
        from custom_components.wavespa import _OBSOLETE_UNIQUE_ID_SUFFIXES

        assert "_spa_has_error" not in _OBSOLETE_UNIQUE_ID_SUFFIXES

    def test_genuinely_removed_entities_are_still_pruned(self) -> None:
        from custom_components.wavespa import _OBSOLETE_UNIQUE_ID_SUFFIXES

        assert "_Heater" in _OBSOLETE_UNIQUE_ID_SUFFIXES
        assert "_mcu_soft_version" in _OBSOLETE_UNIQUE_ID_SUFFIXES
