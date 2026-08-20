"""Binary sensor platform."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import WavespaConfigEntry, WavespaUpdateCoordinator
from .entity import WavespaEntity
from .wavespa.model import WavespaDeviceType

_SPA_CONNECTIVITY_SENSOR_DESCRIPTION = BinarySensorEntityDescription(
    key="spa_connected",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="spa_connected",
)

_SPA_ALERTS_SENSOR_DESCRIPTION = BinarySensorEntityDescription(
    # Key retained from the sensor removed in 13fc624 so that installs
    # upgrading straight from 2.0.0 keep their entity and its history. The old
    # sensor was permanently off, so nothing misleading is inherited.
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
                    DeviceAlertsSensor(
                        coordinator,
                        config_entry,
                        device_id,
                        _SPA_ALERTS_SENSOR_DESCRIPTION,
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
        device = self.wavespa_device
        return device is not None and device.is_online

    @property
    def available(self) -> bool:
        """Return True whenever the coordinator has data.

        Deliberately overrides WavespaEntity.available, which reports False for
        an offline spa. This sensor has to stay readable in order to report
        that the spa is offline.

        A live push transport counts the same as a successful poll, for the
        same reason it does there: otherwise the one entity whose job is to
        report connectivity would itself go unreadable during a cloud outage.
        """
        return self.coordinator.last_update_success or self.coordinator.has_live_push(
            self.device_id
        )


class DeviceAlertsSensor(WavespaEntity, BinarySensorEntity):
    """Whether the spa is currently reporting any fault condition."""

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: WavespaConfigEntry,
        device_id: str,
        entity_description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize sensor."""
        self.entity_description = entity_description
        self._attr_unique_id = f"{device_id}_{entity_description.key}"
        super().__init__(coordinator, config_entry, device_id)

    def _alerts(self) -> dict[str, bool]:
        """Return each alert the spa reports, active or not.

        The reading itself lives on the status model, because the Active alert
        sensor needs exactly the same answer and two entities disagreeing
        about whether something is wrong would be worse than either being
        wrong on its own.
        """
        status = self.status
        return {} if status is None else status.alerts()

    @property
    def is_on(self) -> bool | None:
        """Return True if any alert is active."""
        return any(self._alerts().values())

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return each alert individually, so automations can target one."""
        return self._alerts()
