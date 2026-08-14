"""Home Assistant sensor descriptions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from logging import getLogger
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BUBBLES_WATTS,
    CONF_FILTER_WATTS,
    CONF_HEATER_WATTS,
    DEFAULT_BUBBLES_WATTS,
    DEFAULT_FILTER_WATTS,
    DEFAULT_HEATER_WATTS,
)
from .coordinator import WavespaConfigEntry, WavespaUpdateCoordinator
from .entity import WavespaEntity
from .wavespa.model import WavespaDeviceStatus, WavespaDeviceType

_LOGGER = getLogger(__name__)

# Kept as module constants for the defaults and for tests; the live values come
# from the config entry options, which default to these.
ESTIMATED_HEATER_WATTS = DEFAULT_HEATER_WATTS
ESTIMATED_BUBBLES_WATTS = DEFAULT_BUBBLES_WATTS
ESTIMATED_FILTER_WATTS = DEFAULT_FILTER_WATTS


@dataclass(frozen=True)
class Wattages:
    """The assumed draw of each load, in watts."""

    heater: int
    bubbles: int
    filter: int

    @classmethod
    def from_entry(cls, entry: WavespaConfigEntry) -> Wattages:
        """Read the wattages configured for this entry, falling back to defaults."""
        options = entry.options
        return cls(
            heater=int(options.get(CONF_HEATER_WATTS, DEFAULT_HEATER_WATTS)),
            bubbles=int(options.get(CONF_BUBBLES_WATTS, DEFAULT_BUBBLES_WATTS)),
            filter=int(options.get(CONF_FILTER_WATTS, DEFAULT_FILTER_WATTS)),
        )


# The longest gap between coordinator updates that the energy estimate will
# attribute to the last known wattage. Comfortably above the 5-minute
# WebSocket-active poll interval, so normal operation is unaffected, while an
# outage of hours is not silently booked as steady consumption.
_MAX_INTEGRATION_STEP = timedelta(minutes=15)


def _estimate_watts(
    status: WavespaDeviceStatus | None, wattages: Wattages | None = None
) -> int:
    """Estimate instantaneous power draw in watts from reported spa state."""
    if status is None:
        return 0

    if wattages is None:
        wattages = Wattages(
            ESTIMATED_HEATER_WATTS, ESTIMATED_BUBBLES_WATTS, ESTIMATED_FILTER_WATTS
        )

    watts = 0

    # Heater == 1 only means heating is enabled - the element cycles off once
    # the spa reaches its target, which is exactly when a spa spends most of
    # its day. Billing the full load throughout added roughly 43 kWh a day of
    # fiction to the Energy dashboard. Missing readings count as not heating,
    # so a gap under-reports rather than invents consumption.
    if status.is_heating:
        watts += wattages.heater

    # Filter pump is active when Filter == 1.
    if status.flag("Filter"):
        watts += wattages.filter

    # Bubbles are active when Bubble is non-zero (some models report a
    # level rather than a simple on/off).
    if status.flag("Bubble"):
        watts += wattages.bubbles

    return watts


class EstimatedAssumptionsMixin:
    """Exposes the wattage assumptions behind the estimated sensors.

    Both estimated sensors derive their value from the same constants, so they
    report the same assumptions to let users check the numbers.
    """

    config_entry: WavespaConfigEntry

    @property
    def wattages(self) -> Wattages:
        """The wattages configured for this spa."""
        return Wattages.from_entry(self.config_entry)

    @property
    def extra_state_attributes(self) -> dict[str, int | str]:
        """Return the assumptions used by this estimated sensor."""
        wattages = self.wattages
        return {
            "calculation": "estimated",
            "heater_watts": wattages.heater,
            "bubbles_watts": wattages.bubbles,
            "filter_watts": wattages.filter,
        }


# Entity state comes from the coordinator, so updates are not per-entity
# polling and do not need serialising.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: WavespaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in HA."""
    coordinator = config_entry.runtime_data
    entities: list[WavespaEntity] = []

    for device_id, device in coordinator.api.devices.items():
        # The wattage estimates assume the spa's Heater/Filter/Bubble loads,
        # so they only apply to the device types the other platforms support.
        if device.device_type in [
            WavespaDeviceType.WAVESPA_EU,
            WavespaDeviceType.WAVESPA_US,
        ]:
            entities.extend(
                [
                    EstimatedPowerSensor(
                        coordinator,
                        config_entry,
                        device_id,
                    ),
                    EstimatedEnergySensor(
                        coordinator,
                        config_entry,
                        device_id,
                    ),
                ]
            )

        entities.extend(
            [
                ProtocolVersionSensor(coordinator, config_entry, device_id),
                FilterPercentSensor(
                    coordinator,
                    config_entry,
                    device_id,
                    SensorEntityDescription(
                        key="percent_filter",
                        translation_key="percent_filter",
                        entity_category=EntityCategory.DIAGNOSTIC,
                        native_unit_of_measurement="%",
                    ),
                ),
            ]
        )

    async_add_entities(entities)


class ProtocolVersionSensor(WavespaEntity, SensorEntity):
    """The Gizwits protocol version the spa reports."""

    _attr_translation_key = "protocol_version"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: WavespaConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id)
        # Must stay f"{device_id}_protocol_version": changing it would orphan
        # the existing entity in the registry rather than reuse it.
        self._attr_unique_id = f"{device_id}_protocol_version"

    @property
    def native_value(self) -> StateType:
        """Return the protocol version, or None if the device is unknown."""
        device = self.wavespa_device
        return device.protocol_version if device is not None else None


class FilterPercentSensor(WavespaEntity, SensorEntity):
    """Filter life percentage, derived from the device's current status."""

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: WavespaConfigEntry,
        device_id: str,
        entity_description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self.entity_description = entity_description
        self._attr_unique_id = f"{device_id}_{entity_description.key}"

    @property
    def native_value(self) -> StateType:
        """Return the filter life percentage."""
        if (status := self.status) is not None:
            return status.percent_filter
        return None


class EstimatedPowerSensor(EstimatedAssumptionsMixin, WavespaEntity, SensorEntity):
    """Estimated instantaneous power consumption for a spa.

    This is not measured power. It is a best-effort estimate based on
    reported spa state.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    _attr_translation_key = "estimated_power"

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: WavespaConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the estimated power sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self._attr_unique_id = f"{device_id}_estimated_power"

    @property
    def native_value(self) -> int | None:
        """Return estimated current power draw in watts."""
        if self.status is None:
            return None
        return _estimate_watts(self.status, self.wattages)


class EstimatedEnergySensor(EstimatedAssumptionsMixin, WavespaEntity, RestoreSensor):
    """Estimated cumulative energy consumption for a spa.

    Integrates EstimatedPowerSensor's wattage over time (left-rectangle
    approximation between coordinator updates) into a running kWh total,
    so it can be added to the Home Assistant Energy dashboard, which only
    accepts energy (kWh, total_increasing) entities, not power sensors.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    _attr_translation_key = "estimated_energy"

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: WavespaConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the estimated energy sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self._attr_unique_id = f"{device_id}_estimated_energy"
        self._energy_kwh = 0.0
        self._last_update: datetime | None = None
        self._last_watts = 0

    async def async_added_to_hass(self) -> None:
        """Restore the accumulated total across restarts."""
        await super().async_added_to_hass()

        # Prefer the stored native value: it is always in this sensor's own
        # unit (kWh), whereas the string state is rendered in whatever unit a
        # registry override has applied, which would corrupt the total.
        restored = await self.async_get_last_sensor_data()
        if restored is not None and restored.native_value is not None:
            self._energy_kwh = self._as_kwh(restored.native_value)
        else:
            # Entities that last ran before this sensor stored native data
            # only have the string state to fall back on.
            last_state = await self.async_get_last_state()
            if last_state is not None:
                self._energy_kwh = self._as_kwh(last_state.state)

        self._last_update = dt_util.utcnow()
        self._last_watts = _estimate_watts(self.status, self.wattages)

    @staticmethod
    def _as_kwh(value: Any) -> float:
        """Coerce a restored value to kWh, falling back to 0 if unusable."""
        if value in (None, "unknown", "unavailable"):
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @callback
    def _handle_coordinator_update(self) -> None:
        """Integrate elapsed time at the previous wattage, then advance.

        Home Assistant stops notifying listeners after the first of a run of
        consecutive coordinator failures, so an outage produces no updates at
        all until it ends. Integrating the whole gap on recovery would book the
        entire outage at whatever the spa was drawing before it went quiet - a
        spa heating at 2450 W that drops off for eight hours would add nearly
        20 kWh in one step, whether or not it drew anything.

        So a step longer than _MAX_INTEGRATION_STEP is not counted: we do not
        know what happened during it, and under-reporting is the honest
        failure. A step is also skipped entirely while the coordinator is
        failing, since the reading it would use is already stale.
        """
        now = dt_util.utcnow()

        if self._last_update is not None and self.coordinator.last_update_success:
            elapsed = now - self._last_update
            if elapsed <= _MAX_INTEGRATION_STEP:
                self._energy_kwh += (
                    self._last_watts * (elapsed.total_seconds() / 3600) / 1000
                )
            else:
                _LOGGER.debug(
                    "Skipping %s gap in estimated energy for %s; too long to "
                    "attribute to the last known wattage",
                    elapsed,
                    self.device_id,
                )

        self._last_update = now
        self._last_watts = _estimate_watts(self.status, self.wattages)

        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float:
        """Return the accumulated estimated energy in kWh."""
        return round(self._energy_kwh, 3)
