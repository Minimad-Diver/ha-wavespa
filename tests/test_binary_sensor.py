"""Tests for binary_sensor.py.

This module had no coverage at all, despite holding the most logic-heavy method
in the integration: _all_error_properties applies four separate matching rules
to the raw attrs and feeds both is_on and extra_state_attributes.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import EntityCategory

from custom_components.wavespa.binary_sensor import (
    DeviceConnectivitySensor,
    DeviceErrorsSensor,
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


def _make_errors_sensor(attrs: dict[str, Any] | None) -> DeviceErrorsSensor:
    from custom_components.wavespa.binary_sensor import (
        _SPA_ERRORS_SENSOR_DESCRIPTION,
    )

    coordinator = _make_coordinator(_make_device(), attrs)
    return DeviceErrorsSensor(
        coordinator, MagicMock(), "test_device", _SPA_ERRORS_SENSOR_DESCRIPTION
    )


class TestErrorMatching:
    """Which attributes count as errors, and which deliberately do not."""

    def test_system_err_is_detected(self) -> None:
        sensor = _make_errors_sensor({"system_err1": 1})
        assert sensor.is_on is True
        assert sensor.extra_state_attributes["system_err1"] is True

    def test_multi_digit_system_err_is_detected(self) -> None:
        sensor = _make_errors_sensor({"system_err12": 1})
        assert sensor.is_on is True

    def test_earth_fault_is_detected(self) -> None:
        sensor = _make_errors_sensor({"earth": 1})
        assert sensor.is_on is True

    def test_pool_filter_error_is_detected(self) -> None:
        sensor = _make_errors_sensor({"error": 1})
        assert sensor.is_on is True

    def test_two_digit_e_code_is_detected(self) -> None:
        sensor = _make_errors_sensor({"E01": 1})
        assert sensor.is_on is True

    def test_e32_is_not_an_error(self) -> None:
        """E32 means heating is on and the spa has reached temperature."""
        sensor = _make_errors_sensor({"E32": 1})
        assert sensor.is_on is False
        assert "E32" not in sensor.extra_state_attributes

    def test_longer_e_code_is_not_matched(self) -> None:
        """re.match anchors only at the start, so E123 used to match E\\d{2}."""
        sensor = _make_errors_sensor({"E123": 1})
        assert sensor.is_on is False
        assert "E123" not in sensor.extra_state_attributes

    def test_string_zero_is_not_an_error(self) -> None:
        """bool("0") is True, so a string-typed clear flag looked like a fault."""
        sensor = _make_errors_sensor({"E01": "0", "system_err1": "0", "earth": "0"})
        assert sensor.is_on is False
        assert sensor.extra_state_attributes == {
            "system_err1": False,
            "earth": False,
            "E01": False,
        }

    def test_string_one_is_an_error(self) -> None:
        sensor = _make_errors_sensor({"E01": "1"})
        assert sensor.is_on is True

    def test_unreadable_value_is_not_an_error(self) -> None:
        """An unparseable reading must not invent a fault."""
        sensor = _make_errors_sensor({"E01": "unknown"})
        assert sensor.is_on is False

    def test_unrelated_attributes_are_ignored(self) -> None:
        sensor = _make_errors_sensor(
            {"Heater": 1, "Filter": 1, "Temperature_setup": 40, "Everything": 1}
        )
        assert sensor.is_on is False
        assert sensor.extra_state_attributes == {}


class TestErrorState:
    """is_on reflects whether any detected error is currently active."""

    def test_no_attributes_is_not_an_error(self) -> None:
        sensor = _make_errors_sensor({})
        assert sensor.is_on is False

    def test_inactive_errors_are_reported_but_not_raised(self) -> None:
        """A known-but-clear error appears as False rather than being omitted."""
        sensor = _make_errors_sensor({"system_err1": 0, "earth": 0, "E01": 0})
        assert sensor.is_on is False
        assert sensor.extra_state_attributes == {
            "system_err1": False,
            "earth": False,
            "E01": False,
        }

    def test_one_active_among_many_raises(self) -> None:
        sensor = _make_errors_sensor({"system_err1": 0, "E01": 1, "earth": 0})
        assert sensor.is_on is True

    def test_no_status_reports_no_errors(self) -> None:
        sensor = _make_errors_sensor(None)
        assert sensor.is_on is False
        assert sensor.extra_state_attributes == {}


class TestErrorSensorCategory:
    """The PROBLEM sensor belongs on the main card, not under Diagnostics."""

    def test_errors_sensor_is_not_diagnostic(self) -> None:
        sensor = _make_errors_sensor({})
        assert sensor.entity_category is None

    def test_connectivity_sensor_is_diagnostic(self) -> None:
        from custom_components.wavespa.binary_sensor import (
            _SPA_CONNECTIVITY_SENSOR_DESCRIPTION,
        )

        coordinator = _make_coordinator(_make_device(), {})
        sensor = DeviceConnectivitySensor(
            coordinator,
            MagicMock(),
            "test_device",
            _SPA_CONNECTIVITY_SENSOR_DESCRIPTION,
        )
        assert sensor.entity_category is EntityCategory.DIAGNOSTIC


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
        await async_setup_entry(
            MagicMock(), config_entry, lambda entities: added.extend(entities)
        )
        return added

    async def test_supported_spa_gets_both_sensors(self) -> None:
        entities = await self._setup("Wave_SPA_EU")
        assert any(isinstance(e, DeviceConnectivitySensor) for e in entities)
        assert any(isinstance(e, DeviceErrorsSensor) for e in entities)

    async def test_us_spa_gets_both_sensors(self) -> None:
        entities = await self._setup("Wave_SPA_US")
        assert len(entities) == 2

    async def test_unknown_device_gets_none(self) -> None:
        entities = await self._setup("Something_Else")
        assert entities == []
