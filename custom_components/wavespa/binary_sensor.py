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
from homeassistant.core import HomeAssistant
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import WavespaConfigEntry, WavespaUpdateCoordinator
from .wavespa.model import WavespaDeviceType
from .entity import WavespaEntity

_SPA_CONNECTIVITY_SENSOR_DESCRIPTION = BinarySensorEntityDescription(
    key="spa_connected",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="spa_connected",
)

_SPA_ERRORS_SENSOR_DESCRIPTION = BinarySensorEntityDescription(
    key="spa_has_error",
    translation_key="spa_has_error",
    device_class=BinarySensorDeviceClass.PROBLEM,
)


# Entity state comes from the coordinator, so updates are not per-entity
# polling and do not need serialising.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: WavespaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensor entities."""
    coordinator = config_entry.runtime_data
    entities: list[WavespaEntity] = []

    for device_id, device in coordinator.api.devices.items():
        if device.device_type in [
            WavespaDeviceType.WAVESPA_EU,
            WavespaDeviceType.WAVESPA_US,
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
        config_entry: WavespaConfigEntry,
        device_id: str,
        entity_description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize sensor."""
        self.entity_description = entity_description
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
        config_entry: WavespaConfigEntry,
        device_id: str,
        entity_description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize sensor."""
        self.entity_description = entity_description
        self._attr_unique_id = f"{device_id}_{self.entity_description.key}"
        super().__init__(
            coordinator,
            config_entry,
            device_id,
        )

    def _all_error_properties(self) -> dict[str, bool]:
        """Get all error properties from the device status.

        Flags are read through status.flag() rather than bool(), for the same
        reason as everywhere else: bool("0") is True, so a spa reporting its
        error codes as strings would raise a fault that is not there. An
        unreadable value counts as no error rather than as a fault.
        """
        errors: dict[str, bool] = {}

        status = self.status
        if not status:
            return errors

        # error properties
        for attr in status.attrs:
            if re.fullmatch(r"system_err\d+", attr):
                errors[attr] = status.flag(attr) is True

        # ground fault
        if "earth" in status.attrs:
            errors["earth"] = status.flag("earth") is True

        # spa error properties
        for attr in status.attrs:
            # E32 is reportedly "heating on, target already reached" rather than
            # a fault. It has not been observed on Wave_SPA_EU - a device over
            # its target reports no E-code at all - so the exclusion is kept
            # only in case another model or firmware does send it. It is not
            # what tells the integration whether the spa is heating; that comes
            # from comparing the two temperature readings.
            if attr == "E32":
                continue

            if re.fullmatch(r"E\d{2}", attr):
                errors[attr] = status.flag(attr) is True

        # Pool filter
        if "error" in status.attrs:
            errors["error"] = status.flag("error") is True

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
