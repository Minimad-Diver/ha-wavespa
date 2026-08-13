"""Home Assistant entity descriptions."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import WavespaUpdateCoordinator
from .wavespa.model import WavespaDevice, WavespaDeviceStatus
from .const import DOMAIN


class WavespaEntity(CoordinatorEntity[WavespaUpdateCoordinator]):
    """Wavespa base entity type."""

    # Entity names are relative to the device, so Home Assistant composes the
    # displayed name as "<spa alias> <entity name>". This also keeps entity IDs
    # distinct when more than one spa is set up.
    _attr_has_entity_name = True

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

        # Looked up with .get() rather than indexing. refresh_bindings replaces
        # the device map wholesale, so a response that omits this device would
        # otherwise raise from a property the entity registry reads routinely -
        # noisier than the entity simply going unavailable, which `available`
        # already handles.
        device_info = self.wavespa_device

        info = DeviceInfo(
            identifiers={(DOMAIN, self.device_id)},
            manufacturer="Wavespa",
        )
        if device_info is None:
            return info

        info["name"] = device_info.alias
        info["model"] = device_info.device_type.value
        return info

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
        return self.coordinator.last_update_success and self.wavespa_device is not None
