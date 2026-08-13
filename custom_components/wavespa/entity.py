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
        # Firmware and hardware versions belong on the device page rather than
        # only as separate diagnostic sensors; the bindings response already
        # carries them.
        info["sw_version"] = (
            f"MCU {device_info.mcu_soft_version} / "
            f"Wi-Fi {device_info.wifi_soft_version}"
        )
        info["hw_version"] = (
            f"MCU {device_info.mcu_hard_version} / "
            f"Wi-Fi {device_info.wifi_hard_version}"
        )
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

        is_online is trusted. It was previously ignored here, because on the
        polling-only integration it frequently read false for a spa that was
        working and controllable from the app - the API kept serving state
        regardless, so honouring the flag made entities vanish for no reason.

        With the WebSocket connected the flag is reported directly by the
        server as it changes, rather than inferred from whatever a bindings
        poll happened to catch, and it is reliable. Reporting unavailable is
        also the honest answer: when the spa is off the network the cached
        attributes are stale, and showing them as live state is worse than
        showing nothing.

        The connectivity binary sensor deliberately overrides this - it has to
        stay available in order to report that the spa is offline.
        """
        device = self.wavespa_device
        return (
            self.coordinator.last_update_success
            and device is not None
            and device.is_online
        )
