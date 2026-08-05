"""Binary sensor platform."""

from __future__ import annotations

from collections.abc import Mapping
import re

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WavespaUpdateCoordinator
from .wavespa.model import WavespaDeviceType
from .const import DOMAIN
from .entity import WavespaEntity

_SPA_CONNECTIVITY_SENSOR_DESCRIPTION = BinarySensorEntityDescription(
    key="spa_connected",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
    name="Spa Connected",
)

_SPA_ERRORS_SENSOR_DESCRIPTION = BinarySensorEntityDescription(
    key="spa_has_error",
    name="Spa Errors",
    device_class=BinarySensorDeviceClass.PROBLEM,
)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entities."""
    coordinator: WavespaUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities: list[WavespaEntity] = []

    for device_id, device in coordinator.api.devices.items():
        if device.device_type in [
            WavespaDeviceType.WAVESPA_EU, WavespaDeviceType.WAVESPA_US,
        ]:
            entities.extend(
                [
                    DeviceConnectivitySensor(
                        coordinator,
                        config_entry,
                        device_id,
                        _SPA_CONNECTIVITY_SENSOR_DESCRIPTION,
                    ),
                    DeviceErrorsSensor(
                        coordinator,
                        config_entry,
                        device_id,
                        _SPA_ERRORS_SENSOR_DESCRIPTION,
                    ),
                ]
            )

    async_add_entities(entities)


class DeviceConnectivitySensor(WavespaEntity, BinarySensorEntity):
    """Sensor to indicate whether a device is currently online."""

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        entity_description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize sensor."""
        self.entity_description = entity_description
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_unique_id = f"{device_id}_{self.entity_description.key}"
        super().__init__(
            coordinator,
            config_entry,
            device_id,
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if the spa is online."""
        return self.wavespa_device is not None and self.wavespa_device.is_online

    @property
    def available(self) -> bool:
        """Return True, as the connectivity sensor is always available."""
        return self.coordinator.last_update_success


class DeviceErrorsSensor(WavespaEntity, BinarySensorEntity):
    """Sensor to indicate an error state for all device types."""

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        entity_description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize sensor."""
        self.entity_description = entity_description
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_unique_id = f"{device_id}_{self.entity_description.key}"
        super().__init__(
            coordinator,
            config_entry,
            device_id,
        )

    def _all_error_properties(self) -> dict[str, bool]:
        """Get all error properties from the device status."""
        errors: dict[str, bool] = {}

        if not self.status:
            return errors

        # error properties
        for attr in self.status.attrs:
            if re.match("system_err\\d+", attr):
                errors[attr] = bool(self.status.attrs[attr])

        # ground fault
        if "earth" in self.status.attrs:
            errors["earth"] = bool(self.status.attrs["earth"])

        # spa error properties
        for attr in self.status.attrs:
            # E32: Not actually an error. This means heating is on but the spa has
            #      already reached the desired temperature.
            if attr == "E32":
                continue

            if re.match("E\\d{2}", attr):
                errors[attr] = bool(self.status.attrs[attr])

        # Pool filter
        if "error" in self.status.attrs:
            errors["error"] = bool(self.status.attrs["error"])

        return errors

    @property
    def is_on(self) -> bool | None:
        """Return true if the spa is reporting an error."""
        errors = self._all_error_properties()
        active_errors = {k: v for k, v in errors.items() if v}
        return len(active_errors) > 0

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return more detailed error information."""
        return self._all_error_properties()
