"""Tests for sensor.py: EstimatedPowerSensor and EstimatedEnergySensor.

These tests cover:
- EstimatedPowerSensor.native_value: wattage estimate derived from Heater/
  Filter/Bubble attrs (exercises the shared _estimate_watts() helper)
- EstimatedPowerSensor.extra_state_attributes
- EstimatedEnergySensor: kWh integration across coordinator updates,
  rounding, extra_state_attributes, and RestoreEntity restore behavior
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

from custom_components.wavespa.wavespa.model import (
    WavespaDevice,
    WavespaDeviceStatus,
)
from custom_components.wavespa.wavespa.api import WavespaApiResults
from custom_components.wavespa.sensor import (
    ESTIMATED_BUBBLES_WATTS,
    ESTIMATED_FILTER_WATTS,
    ESTIMATED_HEATER_WATTS,
    EstimatedEnergySensor,
    EstimatedPowerSensor,
)


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
        "Heater": 0,
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


def _make_coordinator(device: WavespaDevice, status: WavespaDeviceStatus | None):
    """Create a mock coordinator with the given device and status."""
    coordinator = MagicMock()
    coordinator.api = MagicMock()
    coordinator.api.devices = {"test_device": device}
    devices = {"test_device": status} if status is not None else {}
    coordinator.data = WavespaApiResults(devices=devices)
    coordinator.last_update_success = True
    coordinator.async_request_refresh = AsyncMock()
    coordinator.async_refresh = AsyncMock()
    return coordinator


# ---------------------------------------------------------------------------
# EstimatedPowerSensor
# ---------------------------------------------------------------------------


class TestEstimatedPowerSensor:
    """Test wattage estimation and reported attributes."""

    def _make_sensor(self, attrs: dict[str, Any] | None) -> EstimatedPowerSensor:
        device = _make_device()
        status = _make_status(attrs) if attrs is not None else None
        coordinator = _make_coordinator(device, status)
        config_entry = MagicMock()
        return EstimatedPowerSensor(
            coordinator, config_entry, "test_device", name="Estimated Power"
        )

    def test_no_loads_active(self):
        """No wattage is reported when nothing is running."""
        sensor = self._make_sensor({"Heater": 0, "Filter": 0, "Bubble": 0})
        assert sensor.native_value == 0

    def test_heater_only(self):
        """Heater contributes ESTIMATED_HEATER_WATTS."""
        sensor = self._make_sensor({"Heater": 1, "Filter": 0, "Bubble": 0})
        assert sensor.native_value == ESTIMATED_HEATER_WATTS

    def test_filter_only(self):
        """Filter contributes ESTIMATED_FILTER_WATTS."""
        sensor = self._make_sensor({"Heater": 0, "Filter": 1, "Bubble": 0})
        assert sensor.native_value == ESTIMATED_FILTER_WATTS

    def test_bubble_only(self):
        """A nonzero Bubble level contributes ESTIMATED_BUBBLES_WATTS."""
        sensor = self._make_sensor({"Heater": 0, "Filter": 0, "Bubble": 1})
        assert sensor.native_value == ESTIMATED_BUBBLES_WATTS

    def test_bubble_higher_level_same_watts(self):
        """A higher Bubble level still only counts as on/off."""
        sensor = self._make_sensor({"Heater": 0, "Filter": 0, "Bubble": 3})
        assert sensor.native_value == ESTIMATED_BUBBLES_WATTS

    def test_all_loads_active(self):
        """All three loads sum together."""
        sensor = self._make_sensor({"Heater": 1, "Filter": 1, "Bubble": 1})
        assert sensor.native_value == (
            ESTIMATED_HEATER_WATTS + ESTIMATED_FILTER_WATTS + ESTIMATED_BUBBLES_WATTS
        )

    def test_native_value_none_when_no_status(self):
        """Returns None when the coordinator has no status for the device."""
        sensor = self._make_sensor(None)
        assert sensor.native_value is None

    def test_extra_state_attributes(self):
        """Reports the wattage assumptions used for the estimate."""
        sensor = self._make_sensor({"Heater": 0, "Filter": 0, "Bubble": 0})
        assert sensor.extra_state_attributes == {
            "calculation": "estimated",
            "heater_watts": ESTIMATED_HEATER_WATTS,
            "bubbles_watts": ESTIMATED_BUBBLES_WATTS,
            "filter_watts": ESTIMATED_FILTER_WATTS,
        }


# ---------------------------------------------------------------------------
# EstimatedEnergySensor
# ---------------------------------------------------------------------------


class TestEstimatedEnergySensor:
    """Test kWh integration, rounding, attributes, and restore behavior."""

    def _make_sensor(self, attrs: dict[str, Any] | None = None) -> EstimatedEnergySensor:
        device = _make_device()
        status = _make_status(attrs if attrs is not None else {})
        coordinator = _make_coordinator(device, status)
        config_entry = MagicMock()
        return EstimatedEnergySensor(
            coordinator, config_entry, "test_device", name="Estimated Energy"
        )

    def test_initial_native_value_is_zero(self):
        """A fresh sensor reports 0 kWh before any update or restore."""
        sensor = self._make_sensor()
        assert sensor.native_value == 0.0

    def test_integrates_wattage_over_elapsed_time(self):
        """Energy accumulates as watts * elapsed_hours / 1000 per update."""
        sensor = self._make_sensor({"Heater": 1, "Filter": 0, "Bubble": 0})

        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t1 = t0 + timedelta(hours=2)
        t2 = t1 + timedelta(hours=1)

        with patch(
            "custom_components.wavespa.sensor.dt_util.utcnow",
            side_effect=[t1, t2],
        ), patch.object(sensor, "async_write_ha_state"):
            sensor._last_update = t0
            sensor._last_watts = ESTIMATED_HEATER_WATTS

            # First update: 2 hours elapsed at ESTIMATED_HEATER_WATTS.
            sensor._handle_coordinator_update()
            expected = ESTIMATED_HEATER_WATTS * 2 / 1000
            assert sensor.native_value == round(expected, 3)

            # Second update: 1 more hour elapsed, still at ESTIMATED_HEATER_WATTS
            # (status attrs unchanged).
            sensor._handle_coordinator_update()
            expected += ESTIMATED_HEATER_WATTS * 1 / 1000
            assert sensor.native_value == round(expected, 3)

    def test_no_integration_on_first_update_without_prior_timestamp(self):
        """Nothing accumulates if _last_update was never set (no prior baseline)."""
        sensor = self._make_sensor({"Heater": 1})

        with patch(
            "custom_components.wavespa.sensor.dt_util.utcnow",
            return_value=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ), patch.object(sensor, "async_write_ha_state"):
            assert sensor._last_update is None
            sensor._handle_coordinator_update()

        assert sensor.native_value == 0.0
        assert sensor._last_watts == ESTIMATED_HEATER_WATTS

    def test_native_value_rounds_to_three_decimals(self):
        """native_value is rounded to 3 decimal places."""
        sensor = self._make_sensor()
        sensor._energy_kwh = 1.23456789
        assert sensor.native_value == 1.235

    def test_extra_state_attributes(self):
        """Reports the same wattage assumptions as EstimatedPowerSensor."""
        sensor = self._make_sensor()
        assert sensor.extra_state_attributes == {
            "calculation": "estimated",
            "heater_watts": ESTIMATED_HEATER_WATTS,
            "bubbles_watts": ESTIMATED_BUBBLES_WATTS,
            "filter_watts": ESTIMATED_FILTER_WATTS,
        }

    async def test_restores_valid_last_state(self):
        """A valid numeric last state restores the accumulated total."""
        sensor = self._make_sensor()
        last_state = MagicMock(state="1.234")

        with patch.object(
            sensor, "async_get_last_state", AsyncMock(return_value=last_state)
        ):
            await sensor.async_added_to_hass()

        assert sensor._energy_kwh == 1.234
        assert sensor._last_update is not None
        assert sensor._last_watts == 0

    async def test_no_restore_when_no_last_state(self):
        """Starts from 0 when there is no previous state to restore."""
        sensor = self._make_sensor()

        with patch.object(
            sensor, "async_get_last_state", AsyncMock(return_value=None)
        ):
            await sensor.async_added_to_hass()

        assert sensor._energy_kwh == 0.0

    async def test_no_restore_when_state_unknown_or_unavailable(self):
        """Sentinel states unknown/unavailable are not treated as data."""
        for sentinel in ("unknown", "unavailable"):
            sensor = self._make_sensor()
            last_state = MagicMock(state=sentinel)

            with patch.object(
                sensor, "async_get_last_state", AsyncMock(return_value=last_state)
            ):
                await sensor.async_added_to_hass()

            assert sensor._energy_kwh == 0.0

    async def test_non_numeric_last_state_falls_back_to_zero(self):
        """A malformed state string does not raise; falls back to 0."""
        sensor = self._make_sensor()
        last_state = MagicMock(state="not-a-number")

        with patch.object(
            sensor, "async_get_last_state", AsyncMock(return_value=last_state)
        ):
            await sensor.async_added_to_hass()

        assert sensor._energy_kwh == 0.0

    async def test_sets_baseline_wattage_after_restore(self):
        """After restore, the baseline wattage reflects current device state."""
        sensor = self._make_sensor({"Heater": 1, "Filter": 1, "Bubble": 0})

        with patch.object(
            sensor, "async_get_last_state", AsyncMock(return_value=None)
        ):
            await sensor.async_added_to_hass()

        assert sensor._last_watts == ESTIMATED_HEATER_WATTS + ESTIMATED_FILTER_WATTS
