"""Home Assistant sensor descriptions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from . import WavespaUpdateCoordinator
from .const import DOMAIN, Icon
from .entity import WavespaEntity
from .wavespa.model import WavespaDevice, WavespaDeviceStatus, WavespaDeviceType

ESTIMATED_HEATER_WATTS = 1800
ESTIMATED_BUBBLES_WATTS = 600
ESTIMATED_FILTER_WATTS = 50


def _estimate_watts(status: WavespaDeviceStatus | None) -> int:
    """Estimate instantaneous power draw in watts from reported spa state."""
    if status is None:
        return 0

    attrs = status.attrs
    watts = 0

    # Heater is active when Heater == 1.
    if int(attrs.get("Heater") or 0) == 1:
        watts += ESTIMATED_HEATER_WATTS

    # Filter pump is active when Filter == 1.
    if int(attrs.get("Filter") or 0) == 1:
        watts += ESTIMATED_FILTER_WATTS

    # Bubbles are active when Bubble is non-zero (some models report a
    # level rather than a simple on/off).
    if int(attrs.get("Bubble") or 0) > 0:
        watts += ESTIMATED_BUBBLES_WATTS

    return watts


class EstimatedAssumptionsMixin:
    """Exposes the wattage assumptions behind the estimated sensors.

    Both estimated sensors derive their value from the same constants, so they
    report the same assumptions to let users check the numbers.
    """

    @property
    def extra_state_attributes(self) -> dict[str, int | str]:
        """Return the assumptions used by this estimated sensor."""
        return {
            "calculation": "estimated",
            "heater_watts": ESTIMATED_HEATER_WATTS,
            "bubbles_watts": ESTIMATED_BUBBLES_WATTS,
            "filter_watts": ESTIMATED_FILTER_WATTS,
        }


@dataclass
class DeviceSensorDescription:
    """An entity description with a function that describes how to derive a value."""

    entity_description: SensorEntityDescription
    value_fn: Callable[[WavespaDevice], StateType]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in HA."""
    coordinator: WavespaUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
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
                        name="Estimated Power",
                    ),
                    EstimatedEnergySensor(
                        coordinator,
                        config_entry,
                        device_id,
                        name="Estimated Energy",
                    ),
                ]
            )

        entities.extend(
            [
                DeviceSensor(
                    coordinator,
                    config_entry,
                    device_id,
                    sensor_description=DeviceSensorDescription(
                        SensorEntityDescription(
                            key="protocol_version",
                            name="Protocol Version",
                            icon=Icon.PROTOCOL,
                            entity_category=EntityCategory.DIAGNOSTIC,
                        ),
                        lambda device: device.protocol_version,
                    ),
                ),
                DeviceSensor(
                    coordinator,
                    config_entry,
                    device_id,
                    sensor_description=DeviceSensorDescription(
                        SensorEntityDescription(
                            key="mcu_soft_version",
                            name="MCU Software Version",
                            icon=Icon.SOFTWARE,
                            entity_category=EntityCategory.DIAGNOSTIC,
                        ),
                        lambda device: device.mcu_soft_version,
                    ),
                ),
                DeviceSensor(
                    coordinator,
                    config_entry,
                    device_id,
                    sensor_description=DeviceSensorDescription(
                        SensorEntityDescription(
                            key="mcu_hard_version",
                            name="MCU Hardware Version",
                            icon=Icon.HARDWARE,
                            entity_category=EntityCategory.DIAGNOSTIC,
                        ),
                        lambda device: device.mcu_hard_version,
                    ),
                ),
                DeviceSensor(
                    coordinator,
                    config_entry,
                    device_id,
                    sensor_description=DeviceSensorDescription(
                        SensorEntityDescription(
                            key="wifi_soft_version",
                            name="Wi-Fi Software Version",
                            icon=Icon.SOFTWARE,
                            entity_category=EntityCategory.DIAGNOSTIC,
                        ),
                        lambda device: device.wifi_soft_version,
                    ),
                ),
                DeviceSensor(
                    coordinator,
                    config_entry,
                    device_id,
                    sensor_description=DeviceSensorDescription(
                        SensorEntityDescription(
                            key="wifi_hard_version",
                            name="Wi-Fi Hardware Version",
                            icon=Icon.HARDWARE,
                            entity_category=EntityCategory.DIAGNOSTIC,
                        ),
                        lambda device: device.wifi_hard_version,
                    ),
                ),
                FilterPercentSensor(
                    coordinator,
                    config_entry,
                    device_id,
                    SensorEntityDescription(
                        key="percent_filter",
                        name="Filter",
                        icon=Icon.FILTER,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        native_unit_of_measurement="%",
                    ),
                ),
            ]
        )

    async_add_entities(entities)


class DeviceSensor(WavespaEntity, SensorEntity):
    """A sensor based on device metadata."""

    sensor_description: DeviceSensorDescription

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        sensor_description: DeviceSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self.sensor_description = sensor_description
        self.entity_description = sensor_description.entity_description
        self._attr_unique_id = f"{device_id}_{self.entity_description.key}"

    @property
    def native_value(self) -> StateType:
        """Return the relevant property."""
        if (device := self.wavespa_device) is not None:
            return self.sensor_description.value_fn(device)
        return None


class FilterPercentSensor(WavespaEntity, SensorEntity):
    """Filter life percentage, derived from the device's current status."""

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: ConfigEntry,
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
    _attr_icon = "mdi:flash"

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        name: str,
    ) -> None:
        """Initialize the estimated power sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self._attr_name = name
        self._attr_unique_id = f"{device_id}_estimated_power"

    @property
    def native_value(self) -> int | None:
        """Return estimated current power draw in watts."""
        if self.status is None:
            return None
        return _estimate_watts(self.status)


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
    _attr_icon = "mdi:lightning-bolt"

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        name: str,
    ) -> None:
        """Initialize the estimated energy sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self._attr_name = name
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
        self._last_watts = _estimate_watts(self.status)

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
        """Integrate elapsed time at the previous wattage, then advance."""
        now = dt_util.utcnow()

        if self._last_update is not None:
            elapsed_hours = (now - self._last_update).total_seconds() / 3600
            self._energy_kwh += self._last_watts * elapsed_hours / 1000

        self._last_update = now
        self._last_watts = _estimate_watts(self.status)

        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float:
        """Return the accumulated estimated energy in kWh."""
        return round(self._energy_kwh, 3)
