"""Tests for entity availability, optimistic switch state, and climate unit.

These tests cover:
- entity.py: available property ignoring unreliable is_online
- switch.py: optimistic state tracking and confirmation-based clearing
- climate.py: temperature_unit derived from device type, and hvac_action
"""

from unittest.mock import MagicMock, AsyncMock, patch
from typing import Any

import pytest

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

    def test_unavailable_when_offline(self):
        """An offline spa reports unavailable rather than stale state.

        is_online used to be ignored here, because on the polling-only
        integration it read false for spas that were plainly working. With the
        WebSocket reporting it directly it is trusted, and showing cached
        attributes as though they were live is worse than showing nothing.
        """
        from custom_components.wavespa.entity import WavespaEntity

        device = _make_device(is_online=False)
        coordinator = _make_coordinator(device, _make_status())
        config_entry = MagicMock()

        entity = WavespaEntity(coordinator, config_entry, "test_device")
        assert entity.available is False

    def test_connectivity_sensor_stays_available_when_offline(self):
        """The sensor reporting the outage must not itself go unavailable."""
        from custom_components.wavespa.binary_sensor import (
            _SPA_CONNECTIVITY_SENSOR_DESCRIPTION,
            DeviceConnectivitySensor,
        )

        device = _make_device(is_online=False)
        coordinator = _make_coordinator(device, _make_status())

        sensor = DeviceConnectivitySensor(
            coordinator,
            MagicMock(),
            "test_device",
            _SPA_CONNECTIVITY_SENSOR_DESCRIPTION,
        )
        assert sensor.available is True
        assert sensor.is_on is False

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
            key="Filter",
            name="Filter",
            value_fn=lambda s: s.flag("Filter"),
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
        status = _make_status({"Filter": 0})
        coordinator = _make_coordinator(device, status)
        config_entry = MagicMock()

        switch = WavespaSwitch(
            coordinator, config_entry, "test_device", self._make_desc()
        )

        # Before toggle: switch reads from coordinator (Filter=0 -> off)
        assert switch.is_on is False

        # Set optimistic state directly (mirrors what async_turn_on does)
        switch._optimistic_state = True
        assert switch.is_on is True

    def test_switch_optimistic_cleared_when_confirmed(self):
        """Optimistic state is cleared once real data confirms it."""
        from custom_components.wavespa.switch import WavespaSwitch

        device = _make_device()
        # Real data agrees with the optimistic value (Filter=1 -> on)
        status = _make_status({"Filter": 1})
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
        # Real data still shows the OLD value (Filter=0 -> off)
        status = _make_status({"Filter": 0})
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


class TestServiceCallFailures:
    """API failures must reach the user as HomeAssistantError.

    Anything else surfaces as an unhandled traceback in the log with nothing
    shown in the UI.
    """

    def _make_switch(self, turn_on_fn):
        from custom_components.wavespa.switch import (
            WavespaSwitch,
            WavespaSwitchEntityDescription,
        )

        desc = WavespaSwitchEntityDescription(
            key="Filter",
            name="Filter",
            value_fn=lambda s: s.flag("Filter"),
            turn_on_fn=turn_on_fn,
            turn_off_fn=AsyncMock(),
        )
        device = _make_device()
        coordinator = _make_coordinator(device, _make_status({"Filter": 0}))
        switch = WavespaSwitch(coordinator, MagicMock(), "test_device", desc)
        switch.hass = MagicMock()
        switch.async_write_ha_state = MagicMock()
        switch.entity_id = "switch.test_filter"
        return switch

    async def test_switch_failure_raises_home_assistant_error(self):
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.wavespa.wavespa.api import WavespaException

        switch = self._make_switch(AsyncMock(side_effect=WavespaException("boom")))

        with pytest.raises(HomeAssistantError, match="Failed to turn on"):
            await switch.async_turn_on()

    async def test_switch_failure_clears_optimistic_state(self):
        """A command that demonstrably failed must not keep showing as applied."""
        from custom_components.wavespa.wavespa.api import WavespaException

        switch = self._make_switch(AsyncMock(side_effect=WavespaException("boom")))

        with pytest.raises(Exception):
            await switch.async_turn_on()

        assert switch._optimistic_state is None
        assert switch.is_on is False

    async def test_climate_failure_raises_home_assistant_error(self):
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.wavespa.climate import WaveSpaThermostat
        from custom_components.wavespa.wavespa.api import WavespaException

        device = _make_device()
        coordinator = _make_coordinator(device, _make_status())
        coordinator.api.spa_set_heat = AsyncMock(
            side_effect=WavespaException("device offline")
        )
        thermostat = WaveSpaThermostat(coordinator, MagicMock(), "test_device")

        with pytest.raises(HomeAssistantError, match="Failed to set the heating mode"):
            from homeassistant.components.climate.const import HVACMode

            await thermostat.async_set_hvac_mode(HVACMode.HEAT)

    async def test_climate_set_temperature_failure_raises(self):
        from homeassistant.const import ATTR_TEMPERATURE
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.wavespa.climate import WaveSpaThermostat
        from custom_components.wavespa.wavespa.api import WavespaException

        device = _make_device()
        coordinator = _make_coordinator(device, _make_status())
        coordinator.api.spa_set_target_temp = AsyncMock(
            side_effect=WavespaException("device offline")
        )
        thermostat = WaveSpaThermostat(coordinator, MagicMock(), "test_device")

        with pytest.raises(
            HomeAssistantError, match="Failed to set the target temperature"
        ):
            await thermostat.async_set_temperature(**{ATTR_TEMPERATURE: 38})


class TestOptimisticExpiry:
    """The expiry is a timer, not a check performed during coordinator updates.

    Checking on update tied the deadline to the polling interval, which is five
    minutes while the WebSocket is connected - so the ten seconds the constant
    promised could be thirty times longer in practice.
    """

    def _make_switch(self):
        from custom_components.wavespa.switch import (
            WavespaSwitch,
            WavespaSwitchEntityDescription,
        )

        desc = WavespaSwitchEntityDescription(
            key="Filter",
            name="Filter",
            value_fn=lambda s: s.flag("Filter"),
            turn_on_fn=AsyncMock(),
            turn_off_fn=AsyncMock(),
        )
        device = _make_device()
        coordinator = _make_coordinator(device, _make_status({"Filter": 0}))
        switch = WavespaSwitch(coordinator, MagicMock(), "test_device", desc)
        switch.hass = MagicMock()
        switch.async_write_ha_state = MagicMock()
        return switch

    async def test_timer_is_armed_on_optimistic_set(self):
        from custom_components.wavespa import switch as switch_module

        switch = self._make_switch()
        with patch.object(switch_module, "async_call_later") as call_later:
            await switch.async_turn_on()

        call_later.assert_called_once()
        assert call_later.call_args[0][1] == switch._OPTIMISTIC_TIMEOUT_SECONDS

    async def test_expiry_reveals_real_state(self):
        """When the timer fires, the switch falls back to the device's value."""
        switch = self._make_switch()
        with patch("custom_components.wavespa.switch.async_call_later"):
            await switch.async_turn_on()

        assert switch.is_on is True  # optimistic
        switch._expire_optimistic(None)
        assert switch._optimistic_state is None
        assert switch.is_on is False  # the device never applied it

    async def test_confirmation_cancels_the_timer(self):
        cancel = MagicMock()
        switch = self._make_switch()
        with patch(
            "custom_components.wavespa.switch.async_call_later", return_value=cancel
        ):
            await switch.async_turn_on()

        # Device confirms the change
        switch.status.attrs["Filter"] = 1
        switch._handle_coordinator_update()

        assert switch._optimistic_state is None
        cancel.assert_called_once()

    async def test_removal_disarms_the_timer(self):
        """A pending timer must not fire against a removed entity."""
        cancel = MagicMock()
        switch = self._make_switch()
        with patch(
            "custom_components.wavespa.switch.async_call_later", return_value=cancel
        ):
            await switch.async_turn_on()

        await switch.async_will_remove_from_hass()
        cancel.assert_called_once()


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


# ---------------------------------------------------------------------------
# climate.py: hvac_action
# ---------------------------------------------------------------------------


class TestClimateHvacAction:
    """Test the reported running action against heater state and temperature."""

    def _make_thermostat(self, attrs: dict[str, Any] | None):
        """Create a WaveSpaThermostat whose status carries the given attrs."""
        from custom_components.wavespa.climate import WaveSpaThermostat

        device = _make_device()
        status = _make_status(attrs) if attrs is not None else None
        coordinator = MagicMock()
        coordinator.api = MagicMock()
        coordinator.api.devices = {"test_device": device}
        devices = {"test_device": status} if status is not None else {}
        coordinator.data = WavespaApiResults(devices=devices)
        coordinator.last_update_success = True
        config_entry = MagicMock()
        return WaveSpaThermostat(coordinator, config_entry, "test_device")

    def test_heating_below_target(self):
        """Heater on and below the target reports HEATING."""
        from homeassistant.components.climate.const import HVACAction

        thermostat = self._make_thermostat(
            {"Heater": 1, "Current_temperature": 30, "Temperature_setup": 40}
        )
        assert thermostat.hvac_action == HVACAction.HEATING

    def test_idle_at_target(self):
        """Heater on and exactly at the target reports IDLE."""
        from homeassistant.components.climate.const import HVACAction

        thermostat = self._make_thermostat(
            {"Heater": 1, "Current_temperature": 40, "Temperature_setup": 40}
        )
        assert thermostat.hvac_action == HVACAction.IDLE

    def test_idle_above_target(self):
        """An overshoot past the target reports IDLE, not HEATING."""
        from homeassistant.components.climate.const import HVACAction

        thermostat = self._make_thermostat(
            {"Heater": 1, "Current_temperature": 41, "Temperature_setup": 40}
        )
        assert thermostat.hvac_action == HVACAction.IDLE

    def test_off_when_heater_off(self):
        """Heater off reports OFF, not IDLE, regardless of temperature.

        IDLE means "on but not currently calling for heat". Reporting it for a
        spa with heating switched off contradicted hvac_mode, which correctly
        returned HVACMode.OFF for the same state.
        """
        from homeassistant.components.climate.const import HVACAction

        thermostat = self._make_thermostat(
            {"Heater": 0, "Current_temperature": 30, "Temperature_setup": 40}
        )
        assert thermostat.hvac_action == HVACAction.OFF

    def test_off_when_heater_off_and_at_target(self):
        """Heater off at target is still OFF rather than IDLE."""
        from homeassistant.components.climate.const import HVACAction

        thermostat = self._make_thermostat(
            {"Heater": 0, "Current_temperature": 40, "Temperature_setup": 40}
        )
        assert thermostat.hvac_action == HVACAction.OFF

    def test_string_zero_heater_is_off(self):
        """A string "0" must not read as on.

        bool("0") is True, so reading the flag with bool() reported a spa that
        was off as heating.
        """
        from homeassistant.components.climate.const import HVACAction, HVACMode

        thermostat = self._make_thermostat(
            {"Heater": "0", "Current_temperature": "30", "Temperature_setup": "40"}
        )
        assert thermostat.hvac_action == HVACAction.OFF
        assert thermostat.hvac_mode == HVACMode.OFF

    def test_string_one_heater_is_heating(self):
        """String readings still work when the spa is genuinely heating."""
        from homeassistant.components.climate.const import HVACAction, HVACMode

        thermostat = self._make_thermostat(
            {"Heater": "1", "Current_temperature": "30", "Temperature_setup": "40"}
        )
        assert thermostat.hvac_action == HVACAction.HEATING
        assert thermostat.hvac_mode == HVACMode.HEAT

    def test_none_when_attrs_missing(self):
        """Missing temperature attributes report unknown rather than guessing."""
        status_attrs = {"Heater": 1, "Current_temperature": 30}
        thermostat = self._make_thermostat(status_attrs)
        thermostat.status.attrs.pop("Temperature_setup")
        assert thermostat.hvac_action is None

    def test_none_when_no_status(self):
        """No status at all reports unknown."""
        thermostat = self._make_thermostat(None)
        assert thermostat.hvac_action is None
