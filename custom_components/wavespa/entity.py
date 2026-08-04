"""Home Assistant entity descriptions."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WavespaUpdateCoordinator
from .wavespa.model import WavespaDevice, WavespaDeviceStatus
from .const import DOMAIN


class WavespaEntity(CoordinatorEntity[WavespaUpdateCoordinator]):
    """Wavespa base entity type."""

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.config_entry = config_entry
        self.device_id = device_id

    @property
    def device_info(self) -> DeviceInfo:
        """Device information for the spa providing this entity."""

        device_info = self.coordinator.api.devices[self.device_id]

        return DeviceInfo(
            identifiers={(DOMAIN, self.device_id)},
            name=device_info.alias,
            model=device_info.device_type.value,
            manufacturer="Wavespa",
        )

    @property
    def wavespa_device(self) -> WavespaDevice | None:
        """Get status data for the spa providing this entity."""
        device: WavespaDevice | None = self.coordinator.api.devices.get(self.device_id)
        return device

    @property
    def status(self) -> WavespaDeviceStatus | None:
        """Get status data for the spa providing this entity."""
        status: WavespaDeviceStatus | None = self.coordinator.data.devices.get(
            self.device_id
        )
        return status

    @property
    def available(self) -> bool:
        """Return True if entity is available.

        Note: is_online from the Gizwits API is unreliable and
        frequently returns false even when the device is functioning and
        controllable via the app. The API continues to return valid state
        data regardless of this flag. We therefore only check that the
        coordinator has data and the device is known.

        """
        return self.coordinator.last_update_success and self.bestway_device is not None
