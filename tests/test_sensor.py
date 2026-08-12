"""Tests for sensor.py: EstimatedPowerSensor and EstimatedEnergySensor.

These tests cover:
- EstimatedPowerSensor.native_value: wattage estimate derived from Heater/
  Filter/Bubble attrs (exercises the shared _estimate_watts() helper)
- EstimatedPowerSensor.extra_state_attributes
- EstimatedEnergySensor: kWh integration across coordinator updates,
  rounding, extra_state_attributes, and RestoreSensor restore behavior
- async_setup_entry: which entities each device type gets
"""

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

from homeassistant.helpers.entity import Entity

from custom_components.wavespa.wavespa.model import (
    WavespaDevice,
    WavespaDeviceStatus,
)
from custom_components.wavespa.wavespa.api import WavespaApiResults
from custom_components.wavespa.const import DOMAIN
from custom_components.wavespa.sensor import (
    ESTIMATED_BUBBLES_WATTS,
    ESTIMATED_FILTER_WATTS,
    ESTIMATED_HEATER_WATTS,
    DeviceSensor,
    EstimatedEnergySensor,
    EstimatedPowerSensor,
    async_setup_entry,
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

    def test_heater_enabled_at_target_draws_nothing(self):
        """A heater sitting at its target is not drawing power.

        Heater == 1 only means heating is *enabled*. The element cycles off on
        reaching the target, which is where a spa spends most of its day, so
        billing the full load throughout added roughly 43 kWh a day of fiction
        to the Energy dashboard.
        """
        sensor = self._make_sensor(
            {
                "Heater": 1,
                "Filter": 0,
                "Bubble": 0,
                "Current_temperature": 40,
                "Temperature_setup": 40,
            }
        )
        assert sensor.native_value == 0

    def test_heater_enabled_above_target_draws_nothing(self):
        """An overshoot past the target is still not drawing power."""
        sensor = self._make_sensor(
            {
                "Heater": 1,
                "Filter": 0,
                "Bubble": 0,
                "Current_temperature": 41,
                "Temperature_setup": 40,
            }
        )
        assert sensor.native_value == 0

    def test_heater_enabled_below_target_draws_full_load(self):
        """Actively heating still bills the full element load."""
        sensor = self._make_sensor(
            {
                "Heater": 1,
                "Filter": 0,
                "Bubble": 0,
                "Current_temperature": 39,
                "Temperature_setup": 40,
            }
        )
        assert sensor.native_value == ESTIMATED_HEATER_WATTS

    def test_heater_at_target_still_counts_other_loads(self):
        """Only the heater is dropped - the pump and bubbles keep drawing."""
        sensor = self._make_sensor(
            {
                "Heater": 1,
                "Filter": 1,
                "Bubble": 1,
                "Current_temperature": 40,
                "Temperature_setup": 40,
            }
        )
        assert sensor.native_value == (ESTIMATED_FILTER_WATTS + ESTIMATED_BUBBLES_WATTS)

    def test_missing_temperature_omits_heater(self):
        """A gap in the readings under-reports rather than inventing consumption."""
        sensor = self._make_sensor({"Heater": 1, "Filter": 1, "Bubble": 0})
        sensor.status.attrs.pop("Temperature_setup")
        assert sensor.native_value == ESTIMATED_FILTER_WATTS

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

    def _make_sensor(
        self, attrs: dict[str, Any] | None = None
    ) -> EstimatedEnergySensor:
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

        with (
            patch(
                "custom_components.wavespa.sensor.dt_util.utcnow",
                side_effect=[t1, t2],
            ),
            patch.object(sensor, "async_write_ha_state"),
        ):
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

        with (
            patch(
                "custom_components.wavespa.sensor.dt_util.utcnow",
                return_value=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            patch.object(sensor, "async_write_ha_state"),
        ):
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

    async def _add_to_hass(
        self,
        sensor: EstimatedEnergySensor,
        sensor_data: Any = None,
        last_state: Any = None,
    ) -> None:
        """Run async_added_to_hass with both restore sources stubbed out."""
        with (
            patch.object(
                sensor,
                "async_get_last_sensor_data",
                AsyncMock(return_value=sensor_data),
            ),
            patch.object(
                sensor, "async_get_last_state", AsyncMock(return_value=last_state)
            ),
        ):
            await sensor.async_added_to_hass()

    async def test_restores_native_value(self):
        """The stored native value restores the accumulated total."""
        sensor = self._make_sensor()

        await self._add_to_hass(sensor, sensor_data=MagicMock(native_value=1.234))

        assert sensor._energy_kwh == 1.234
        assert sensor._last_update is not None
        assert sensor._last_watts == 0

    async def test_native_value_preferred_over_state(self):
        """The native value wins over the displayed state.

        The state string is rendered in whatever unit a registry override
        applies, so restoring from it would corrupt the kWh total.
        """
        sensor = self._make_sensor()

        await self._add_to_hass(
            sensor,
            sensor_data=MagicMock(native_value=1.5),
            last_state=MagicMock(state="1500.0"),
        )

        assert sensor._energy_kwh == 1.5

    async def test_falls_back_to_last_state_without_native_data(self):
        """Entities stored before native data was written still restore."""
        sensor = self._make_sensor()

        await self._add_to_hass(sensor, last_state=MagicMock(state="1.234"))

        assert sensor._energy_kwh == 1.234

    async def test_no_restore_when_nothing_stored(self):
        """Starts from 0 when there is no previous data to restore."""
        sensor = self._make_sensor()

        await self._add_to_hass(sensor)

        assert sensor._energy_kwh == 0.0

    async def test_no_restore_when_state_unknown_or_unavailable(self):
        """Sentinel states unknown/unavailable are not treated as data."""
        for sentinel in ("unknown", "unavailable"):
            sensor = self._make_sensor()

            await self._add_to_hass(sensor, last_state=MagicMock(state=sentinel))

            assert sensor._energy_kwh == 0.0

    async def test_non_numeric_last_state_falls_back_to_zero(self):
        """A malformed state string does not raise; falls back to 0."""
        sensor = self._make_sensor()

        await self._add_to_hass(sensor, last_state=MagicMock(state="not-a-number"))

        assert sensor._energy_kwh == 0.0

    async def test_non_numeric_native_value_falls_back_to_zero(self):
        """A malformed native value does not raise; falls back to 0."""
        sensor = self._make_sensor()

        await self._add_to_hass(sensor, sensor_data=MagicMock(native_value="broken"))

        assert sensor._energy_kwh == 0.0

    async def test_sets_baseline_wattage_after_restore(self):
        """After restore, the baseline wattage reflects current device state."""
        sensor = self._make_sensor({"Heater": 1, "Filter": 1, "Bubble": 0})

        await self._add_to_hass(sensor)

        assert sensor._last_watts == ESTIMATED_HEATER_WATTS + ESTIMATED_FILTER_WATTS


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------


class TestSetupEntry:
    """Test which entities get created for each device type."""

    async def _setup(self, product_name: str) -> list[Any]:
        """Run async_setup_entry for one device and return the added entities."""
        device = _make_device(product_name=product_name)
        coordinator = _make_coordinator(device, _make_status())
        hass = MagicMock()
        hass.data = {DOMAIN: {"test_entry": coordinator}}
        config_entry = MagicMock()
        config_entry.entry_id = "test_entry"

        added: list[Any] = []

        def add_entities(
            new_entities: Iterable[Entity], update_before_add: bool = False
        ) -> None:
            added.extend(new_entities)

        await async_setup_entry(hass, config_entry, add_entities)
        return added

    async def test_spa_gets_estimated_sensors(self):
        """A supported spa gets both estimate sensors."""
        entities = await self._setup("Wave_SPA_EU")

        assert any(isinstance(e, EstimatedPowerSensor) for e in entities)
        assert any(isinstance(e, EstimatedEnergySensor) for e in entities)

    async def test_unknown_device_skips_estimated_sensors(self):
        """An unsupported device type gets no wattage estimates.

        The estimates assume the spa's Heater/Filter/Bubble loads, so they
        follow the same device-type gate as switch/climate/binary_sensor.
        """
        entities = await self._setup("Something_Else")

        assert not any(
            isinstance(e, (EstimatedPowerSensor, EstimatedEnergySensor))
            for e in entities
        )
        # Diagnostic sensors are not gated, so they are still created.
        assert any(isinstance(e, DeviceSensor) for e in entities)
