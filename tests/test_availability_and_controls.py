"""Tests for entity availability, optimistic switch state, and climate unit.

These tests cover:
- entity.py: available property ignoring unreliable is_online
- switch.py: optimistic state tracking and confirmation-based clearing
- climate.py: temperature_unit derived from device type
"""

from unittest.mock import MagicMock, AsyncMock, patch
from typing import Any

from custom_components.wavespa.wavespa.model import (
    WavespaDevice,
    WavespaDeviceStatus,
)
from custom_components.wavespa.wavespa.api import WavespaApiResults


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_status(attrs: dict[str, Any] | None = None) -> WavespaDeviceStatus:
    default_attrs = {
        "Heater": 1,
        "Filter": 0,
        "Bubble": 0,
        "locked": 0,
        "Current_temperature": 30,
        "Temperature_setup": 40,
        "Time_filter": 5000,
    }
    if attrs:
        default_attrs.update(attrs)
    return WavespaDeviceStatus(timestamp=1000, attrs=default_attrs)


def _make_coordinator(device: WavespaDevice, status: WavespaDeviceStatus):
    """Create a mock coordinator with the given device and status."""
    coordinator = MagicMock()
    coordinator.api = MagicMock()
    coordinator.api.devices = {"test_device": device}
    coordinator.data = WavespaApiResults(devices={"test_device": status})
    coordinator.last_update_success = True
    coordinator.async_request_refresh = AsyncMock()
    coordinator.async_refresh = AsyncMock()
    return coordinator


# ---------------------------------------------------------------------------
# entity.py: available property
# ---------------------------------------------------------------------------


class TestEntityAvailability:
    """Test that entity availability does NOT depend on is_online."""

    def test_available_when_online(self):
        """Entity is available when device is online."""
        from custom_components.wavespa.entity import WavespaEntity

        device = _make_device(is_online=True)
        coordinator = _make_coordinator(device, _make_status())
        config_entry = MagicMock()

        entity = WavespaEntity(coordinator, config_entry, "test_device")
        assert entity.available is True

    def test_available_when_offline(self):
        """Entity is available even when is_online is False.

        This is the core fix: the Gizwits API reports is_online=False
        unreliably, but the device data is still valid.
        """
        from custom_components.wavespa.entity import WavespaEntity

        device = _make_device(is_online=False)
        coordinator = _make_coordinator(device, _make_status())
        config_entry = MagicMock()

        entity = WavespaEntity(coordinator, config_entry, "test_device")
        assert entity.available is True

    def test_unavailable_when_no_device(self):
        """Entity is unavailable when device is not in coordinator."""
        from custom_components.wavespa.entity import WavespaEntity

        coordinator = MagicMock()
        coordinator.api = MagicMock()
        coordinator.api.devices = {}  # No devices
        coordinator.last_update_success = True
        config_entry = MagicMock()

        entity = WavespaEntity(coordinator, config_entry, "test_device")
        assert entity.available is False

    def test_unavailable_when_coordinator_fails(self):
        """Entity is unavailable when coordinator update failed."""
        from custom_components.wavespa.entity import WavespaEntity

        device = _make_device(is_online=True)
        coordinator = _make_coordinator(device, _make_status())
        coordinator.last_update_success = False
        config_entry = MagicMock()

        entity = WavespaEntity(coordinator, config_entry, "test_device")
        assert entity.available is False


# ---------------------------------------------------------------------------
# switch.py: optimistic state tracking
# ---------------------------------------------------------------------------


class TestSwitchOptimistic:
    """Test that switches use optimistic state updates."""

    def _make_desc(self):
        from custom_components.wavespa.switch import WavespaSwitchEntityDescription

        return WavespaSwitchEntityDescription(
            key="Heater",
            name="Heater",
            value_fn=lambda s: bool(s.attrs["Heater"]),
            turn_on_fn=AsyncMock(),
            turn_off_fn=AsyncMock(),
        )

    def test_switch_has_assumed_state(self):
        """Switch should declare assumed_state for optimistic updates."""
        from custom_components.wavespa.switch import WavespaSwitch

        device = _make_device()
        coordinator = _make_coordinator(device, _make_status())
        config_entry = MagicMock()

        switch = WavespaSwitch(
            coordinator, config_entry, "test_device", self._make_desc()
        )
        assert switch._attr_assumed_state is True

    def test_switch_optimistic_turn_on(self):
        """Switch shows ON immediately after turn_on, before API responds."""
        from custom_components.wavespa.switch import WavespaSwitch

        device = _make_device()
        status = _make_status({"Heater": 0})
        coordinator = _make_coordinator(device, status)
        config_entry = MagicMock()

        switch = WavespaSwitch(
            coordinator, config_entry, "test_device", self._make_desc()
        )

        # Before toggle: switch reads from coordinator (Heater=0 -> off)
        assert switch.is_on is False

        # Set optimistic state directly (mirrors what async_turn_on does)
        switch._optimistic_state = True
        assert switch.is_on is True

    def test_switch_optimistic_cleared_when_confirmed(self):
        """Optimistic state is cleared once real data confirms it."""
        from custom_components.wavespa.switch import WavespaSwitch

        device = _make_device()
        # Real data agrees with the optimistic value (Heater=1 -> on)
        status = _make_status({"Heater": 1})
        coordinator = _make_coordinator(device, status)
        config_entry = MagicMock()

        switch = WavespaSwitch(
            coordinator, config_entry, "test_device", self._make_desc()
        )
        switch._optimistic_state = True  # Optimistic says ON, matches real data

        # Simulate coordinator update — patch async_write_ha_state since
        # there's no real HA instance in unit tests
        with patch.object(switch, "async_write_ha_state"):
            switch._handle_coordinator_update()

        # Confirmed value matches optimistic value, so it is cleared
        assert switch._optimistic_state is None
        assert switch.is_on is True

    def test_switch_optimistic_retained_until_confirmed(self):
        """Optimistic state is retained while real data still disagrees.

        This prevents the UI flashing back to the old state before the
        device has applied the change.
        """
        from custom_components.wavespa.switch import WavespaSwitch

        device = _make_device()
        # Real data still shows the OLD value (Heater=0 -> off)
        status = _make_status({"Heater": 0})
        coordinator = _make_coordinator(device, status)
        config_entry = MagicMock()

        switch = WavespaSwitch(
            coordinator, config_entry, "test_device", self._make_desc()
        )
        switch._optimistic_state = True  # Optimistic says ON, real data still off

        with patch.object(switch, "async_write_ha_state"):
            switch._handle_coordinator_update()

        # Not yet confirmed, so the optimistic value is kept
        assert switch._optimistic_state is True
        assert switch.is_on is True


# ---------------------------------------------------------------------------
# climate.py: temperature unit from device type
# ---------------------------------------------------------------------------


class TestClimateTemperatureUnit:
    """Test that temperature_unit is derived from the device type."""

    def _make_thermostat(self, product_name: str = "Wave_SPA_EU"):
        """Create a WaveSpaThermostat for a device of the given product name."""
        from custom_components.wavespa.climate import WaveSpaThermostat

        device = _make_device(product_name=product_name)
        coordinator = _make_coordinator(device, _make_status())
        config_entry = MagicMock()
        return WaveSpaThermostat(coordinator, config_entry, "test_device")

    def _make_thermostat_no_status(self):
        """Create a thermostat whose coordinator has no status for the device."""
        from custom_components.wavespa.climate import WaveSpaThermostat

        device = _make_device()
        coordinator = MagicMock()
        coordinator.api = MagicMock()
        coordinator.api.devices = {"test_device": device}
        coordinator.data = WavespaApiResults(devices={})
        coordinator.last_update_success = True
        config_entry = MagicMock()
        return WaveSpaThermostat(coordinator, config_entry, "test_device")

    def test_temperature_unit_eu_is_celsius(self):
        """An EU device reports in Celsius."""
        from homeassistant.const import UnitOfTemperature

        thermostat = self._make_thermostat(product_name="Wave_SPA_EU")
        assert thermostat.temperature_unit == str(UnitOfTemperature.CELSIUS)

    def test_temperature_unit_us_is_fahrenheit(self):
        """A US device reports in Fahrenheit."""
        from homeassistant.const import UnitOfTemperature

        thermostat = self._make_thermostat(product_name="Wave_SPA_US")
        assert thermostat.temperature_unit == str(UnitOfTemperature.FAHRENHEIT)

    def test_temperature_unit_unknown_defaults_celsius(self):
        """An unrecognised device type defaults to Celsius."""
        from homeassistant.const import UnitOfTemperature

        thermostat = self._make_thermostat(product_name="Something_Else")
        assert thermostat.temperature_unit == str(UnitOfTemperature.CELSIUS)

    def test_temperature_unit_with_no_status(self):
        """Returns Celsius when status is None."""
        from homeassistant.const import UnitOfTemperature

        thermostat = self._make_thermostat_no_status()
        assert thermostat.temperature_unit == str(UnitOfTemperature.CELSIUS)
