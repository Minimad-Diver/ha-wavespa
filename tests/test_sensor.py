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

from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import Entity

from custom_components.wavespa.wavespa.model import (
    WavespaDevice,
    WavespaDeviceStatus,
)
from custom_components.wavespa.wavespa.api import WavespaApiResults
from custom_components.wavespa.sensor import (
    ESTIMATED_BUBBLES_WATTS,
    ESTIMATED_FILTER_WATTS,
    ESTIMATED_HEATER_WATTS,
    ActiveAlertSensor,
    ProtocolVersionSensor,
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


def _make_config_entry(options: dict[str, Any] | None = None) -> MagicMock:
    """A config entry whose options behave like a real (usually empty) mapping.

    The estimated sensors read their wattages from entry.options, so a bare
    MagicMock would hand them mock objects rather than numbers.
    """
    config_entry = MagicMock()
    config_entry.options = options or {}
    return config_entry


# ---------------------------------------------------------------------------
# EstimatedPowerSensor
# ---------------------------------------------------------------------------


class TestEstimatedPowerSensor:
    """Test wattage estimation and reported attributes."""

    def _make_sensor(self, attrs: dict[str, Any] | None) -> EstimatedPowerSensor:
        device = _make_device()
        status = _make_status(attrs) if attrs is not None else None
        coordinator = _make_coordinator(device, status)
        config_entry = _make_config_entry()
        return EstimatedPowerSensor(coordinator, config_entry, "test_device")

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

    def test_configured_wattages_are_used(self):
        """Options override the defaults, so a different spa can be corrected."""
        from custom_components.wavespa.const import (
            CONF_BUBBLES_WATTS,
            CONF_FILTER_WATTS,
            CONF_HEATER_WATTS,
        )

        device = _make_device()
        coordinator = _make_coordinator(
            device,
            _make_status({"Heater": 1, "Filter": 1, "Bubble": 1}),
        )
        config_entry = _make_config_entry(
            {
                CONF_HEATER_WATTS: 2400,
                CONF_BUBBLES_WATTS: 750,
                CONF_FILTER_WATTS: 40,
            }
        )
        sensor = EstimatedPowerSensor(coordinator, config_entry, "test_device")

        assert sensor.native_value == 2400 + 750 + 40

    def test_configured_wattages_are_reported_in_attributes(self):
        """The attributes must describe the numbers actually in use."""
        from custom_components.wavespa.const import CONF_HEATER_WATTS

        device = _make_device()
        coordinator = _make_coordinator(device, _make_status())
        config_entry = _make_config_entry({CONF_HEATER_WATTS: 2400})
        sensor = EstimatedPowerSensor(coordinator, config_entry, "test_device")

        attrs = sensor.extra_state_attributes
        assert attrs["heater_watts"] == 2400
        # Unset options still fall back to the defaults
        assert attrs["filter_watts"] == ESTIMATED_FILTER_WATTS

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
        config_entry = _make_config_entry()
        return EstimatedEnergySensor(coordinator, config_entry, "test_device")

    def test_initial_native_value_is_zero(self):
        """A fresh sensor reports 0 kWh before any update or restore."""
        sensor = self._make_sensor()
        assert sensor.native_value == 0.0

    def test_integrates_wattage_over_elapsed_time(self):
        """Energy accumulates as watts * elapsed_hours / 1000 per update."""
        sensor = self._make_sensor({"Heater": 1, "Filter": 0, "Bubble": 0})

        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=10)
        t2 = t1 + timedelta(minutes=5)

        with (
            patch(
                "custom_components.wavespa.sensor.dt_util.utcnow",
                side_effect=[t1, t2],
            ),
            patch.object(sensor, "async_write_ha_state"),
        ):
            sensor._last_update = t0
            sensor._last_watts = ESTIMATED_HEATER_WATTS

            # First update: 10 minutes elapsed at ESTIMATED_HEATER_WATTS.
            sensor._handle_coordinator_update()
            expected = ESTIMATED_HEATER_WATTS * (10 / 60) / 1000
            assert sensor.native_value == round(expected, 3)

            # Second update: 5 more minutes elapsed, still at
            # ESTIMATED_HEATER_WATTS (status attrs unchanged).
            sensor._handle_coordinator_update()
            expected += ESTIMATED_HEATER_WATTS * (5 / 60) / 1000
            assert sensor.native_value == round(expected, 3)

    def test_outage_is_not_backfilled(self):
        """A long gap is not booked at the pre-outage wattage.

        Home Assistant stops notifying listeners after the first of a run of
        consecutive coordinator failures, so an outage produces no updates and
        then one update on recovery. Integrating that whole gap would add the
        entire outage at whatever the spa was drawing before it went quiet - a
        spa heating at 2450 W offline for 8 hours would book nearly 20 kWh in
        one step, whether or not it drew anything.
        """
        sensor = self._make_sensor({"Heater": 1, "Filter": 1, "Bubble": 1})

        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        recovery = t0 + timedelta(hours=8)

        with (
            patch(
                "custom_components.wavespa.sensor.dt_util.utcnow",
                return_value=recovery,
            ),
            patch.object(sensor, "async_write_ha_state"),
        ):
            sensor._last_update = t0
            sensor._last_watts = 2450

            sensor._handle_coordinator_update()

        assert sensor.native_value == 0.0

    def test_no_accrual_while_the_coordinator_is_failing(self):
        """The first failed update must not accrue at a now-stale reading."""
        sensor = self._make_sensor({"Heater": 1})
        sensor.coordinator.last_update_success = False

        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

        with (
            patch(
                "custom_components.wavespa.sensor.dt_util.utcnow",
                return_value=t0 + timedelta(minutes=5),
            ),
            patch.object(sensor, "async_write_ha_state"),
        ):
            sensor._last_update = t0
            sensor._last_watts = ESTIMATED_HEATER_WATTS

            sensor._handle_coordinator_update()

        assert sensor.native_value == 0.0

    def test_gap_at_the_cap_still_counts(self):
        """The normal 5-minute poll interval is well inside the cap."""
        sensor = self._make_sensor({"Heater": 1, "Filter": 0, "Bubble": 0})

        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

        with (
            patch(
                "custom_components.wavespa.sensor.dt_util.utcnow",
                return_value=t0 + timedelta(minutes=5),
            ),
            patch.object(sensor, "async_write_ha_state"),
        ):
            sensor._last_update = t0
            sensor._last_watts = ESTIMATED_HEATER_WATTS

            sensor._handle_coordinator_update()

        assert sensor.native_value == round(ESTIMATED_HEATER_WATTS * (5 / 60) / 1000, 3)

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
        config_entry = _make_config_entry()
        config_entry.entry_id = "test_entry"
        config_entry.runtime_data = coordinator

        added: list[Any] = []

        def add_entities(
            new_entities: Iterable[Entity],
            update_before_add: bool = False,
            *,
            config_subentry_id: str | None = None,
        ) -> None:
            added.extend(new_entities)

        await async_setup_entry(hass, config_entry, add_entities)
        return added

    async def test_spa_gets_estimated_sensors(self):
        """A supported spa gets both estimate sensors."""
        entities = await self._setup("Wave_SPA_EU")

        assert any(isinstance(e, EstimatedPowerSensor) for e in entities)
        assert any(isinstance(e, EstimatedEnergySensor) for e in entities)

    async def test_spa_gets_an_active_alert_sensor(self):
        entities = await self._setup("Wave_SPA_EU")

        assert any(isinstance(e, ActiveAlertSensor) for e in entities)

    async def test_unknown_device_gets_no_active_alert_sensor(self):
        """The alert datapoints are this product's, named from its own
        definition, so an unrecognised device gets no claim about them."""
        entities = await self._setup("Something_Else")

        assert not any(isinstance(e, ActiveAlertSensor) for e in entities)

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
        assert any(isinstance(e, ProtocolVersionSensor) for e in entities)


class TestActiveAlertSensor:
    """Naming the fault, rather than only flagging that there is one.

    The values pinned here come from a real spa: Overtime_filter arrived as 1
    with Time_filter at 10080, which is what prompted the entity.
    """

    def _sensor(self, attrs: dict[str, Any] | None = None) -> ActiveAlertSensor:
        device = _make_device()
        status = None if attrs is None else _make_status(attrs)
        coordinator = _make_coordinator(device, status)
        return ActiveAlertSensor(coordinator, _make_config_entry(), "test_device")

    def test_a_healthy_spa_reports_none(self):
        sensor = self._sensor({"Overtime_filter": 0, "Superheat": 0, "Undercooling": 0})

        assert sensor.native_value == "none"

    def test_an_expired_filter_is_named(self):
        """The case that prompted this: "Problem" does not say what to do,
        and "Filter expired" says change the filter."""
        sensor = self._sensor({"Overtime_filter": 1, "Superheat": 0, "Undercooling": 0})

        assert sensor.native_value == "filter_expired"

    def test_overheating_is_named(self):
        sensor = self._sensor({"Superheat": 1})

        assert sensor.native_value == "overheating"

    def test_water_too_cold_is_named(self):
        sensor = self._sensor({"Undercooling": 1})

        assert sensor.native_value == "too_cold"

    def test_several_at_once_are_not_reduced_to_one(self):
        """An enum holds one value, so naming one of several would hide the
        rest. The attributes still carry each of them."""
        sensor = self._sensor({"Overtime_filter": 1, "Superheat": 1})

        assert sensor.native_value == "multiple"
        assert sensor.extra_state_attributes == {
            "Overtime_filter": True,
            "Superheat": True,
        }

    def test_flags_sent_as_text_are_read_as_numbers(self):
        """bool("0") is True, so a spa sending its flags as strings would
        otherwise report a fault that is not there."""
        sensor = self._sensor({"Overtime_filter": "0"})

        assert sensor.native_value == "none"

    def test_an_alert_the_spa_does_not_send_is_not_invented(self):
        """Absent is not the same as clear - this model may not have it, and
        reporting it as clear would claim a reading we never got."""
        sensor = self._sensor({"Overtime_filter": 0})

        assert sensor.extra_state_attributes == {"Overtime_filter": False}
        assert sensor.native_value == "none"

    def test_no_status_is_unknown_not_healthy(self):
        """Reporting "none" with nothing to go on would say the spa is fine
        when we simply have not heard from it."""
        sensor = self._sensor()

        assert sensor.native_value is None

    def test_every_reported_state_is_a_declared_option(self):
        """Home Assistant rejects an enum state outside its options list."""
        for attrs in (
            {},
            {"Overtime_filter": 1},
            {"Superheat": 1},
            {"Undercooling": 1},
            {"Overtime_filter": 1, "Undercooling": 1},
        ):
            sensor = self._sensor(attrs)
            assert sensor.native_value in (sensor.options or [])


class TestProtocolVersionSensor:
    """Replaced the generic DeviceSensor machinery when it had one user left."""

    def _make_sensor(self, device: WavespaDevice | None = None):
        from custom_components.wavespa.sensor import ProtocolVersionSensor

        coordinator = _make_coordinator(device or _make_device(), _make_status())
        return ProtocolVersionSensor(coordinator, _make_config_entry(), "test_device")

    def test_reports_the_protocol_version(self):
        assert self._make_sensor().native_value == 2

    def test_unique_id_is_unchanged(self):
        """Changing this would orphan the existing entity rather than reuse it."""
        assert self._make_sensor()._attr_unique_id == "test_device_protocol_version"

    def test_is_diagnostic(self):
        assert self._make_sensor().entity_category is EntityCategory.DIAGNOSTIC

    def test_none_when_the_device_is_unknown(self):
        """A bindings refresh that drops the device must not raise."""
        from custom_components.wavespa.sensor import ProtocolVersionSensor

        coordinator = _make_coordinator(_make_device(), _make_status())
        coordinator.api.devices = {}
        sensor = ProtocolVersionSensor(coordinator, _make_config_entry(), "test_device")

        assert sensor.native_value is None
