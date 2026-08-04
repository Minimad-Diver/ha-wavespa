"""Switch platform support."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WavespaUpdateCoordinator
from .wavespa.api import WavespaApi
from .wavespa.model import WavespaDeviceStatus, WavespaDeviceType
from .const import DOMAIN, Icon
from .entity import WavespaEntity


@dataclass(frozen=True, kw_only=True)
class WavespaSwitchEntityDescription(SwitchEntityDescription):
    """Entity description for switches."""

    value_fn: Callable[[WavespaDeviceStatus], bool]
    turn_on_fn: Callable[[WavespaApi, str], Awaitable[None]]
    turn_off_fn: Callable[[WavespaApi, str], Awaitable[None]]

_AIRJET_SPA_POWER_SWITCH = WavespaSwitchEntityDescription(
    key="Heater",
    name="Heater",
    icon=Icon.POWER,
    value_fn=lambda s: bool(s.attrs["Heater"]),
    turn_on_fn=lambda api, device_id: api.airjet_spa_set_power(device_id, True),
    turn_off_fn=lambda api, device_id: api.airjet_spa_set_power(device_id, False),
)

_AIRJET_SPA_FILTER_SWITCH = WavespaSwitchEntityDescription(
    key="Filter",
    name="Filter",
    icon=Icon.FILTER,
    value_fn=lambda s: bool(s.attrs["Filter"]),
    turn_on_fn=lambda api, device_id: api.airjet_spa_set_filter(device_id, True),
    turn_off_fn=lambda api, device_id: api.airjet_spa_set_filter(device_id, False),
)

_AIRJET_SPA_BUBBLES_SWITCH = WavespaSwitchEntityDescription(
    key="Bubble",
    name="Bubbles",
    icon=Icon.BUBBLES,
    value_fn=lambda s: bool(s.attrs["Bubble"]),
    turn_on_fn=lambda api, device_id: api.airjet_spa_set_bubbles(device_id, True),
    turn_off_fn=lambda api, device_id: api.airjet_spa_set_bubbles(device_id, False),
)

_AIRJET_SPA_LOCK_SWITCH = WavespaSwitchEntityDescription(
    key="spa_locked",
    name="Spa Locked",
    icon=Icon.LOCK,
    value_fn=lambda s: bool(s.attrs["locked"]),
    turn_on_fn=lambda api, device_id: api.airjet_spa_set_locked(device_id, True),
    turn_off_fn=lambda api, device_id: api.airjet_spa_set_locked(device_id, False),
)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities."""
    coordinator: WavespaUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities: list[WavespaEntity] = []

    for device_id, device in coordinator.api.devices.items():
        # if device.device_type == WavespaDeviceType.WAVESPA_EU:

        if device.device_type in [
            WavespaDeviceType.WAVESPA_EU, WavespaDeviceType.WAVESPA_US,
        ]:
            entities.extend(
                [
                    WavespaSwitch(
                        coordinator,
                        config_entry,
                        device_id,
                        _AIRJET_SPA_POWER_SWITCH
                    ),
                    WavespaSwitch(
                        coordinator,
                        config_entry,
                        device_id,
                        _AIRJET_SPA_FILTER_SWITCH,
                    ),
                    WavespaSwitch(
                        coordinator,
                        config_entry,
                        device_id,
                        _AIRJET_SPA_BUBBLES_SWITCH,
                    ),
                ]
            )

    async_add_entities(entities)


class WavespaSwitch(WavespaEntity, SwitchEntity):
    """Wavespa switch entity."""

    entity_description: WavespaSwitchEntityDescription
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        description: WavespaSwitchEntityDescription,
    ) -> None:
        """Initialize switch."""
        super().__init__(coordinator, config_entry, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._optimistic_state: bool | None = None

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        if status := self.status:
            return self.entity_description.value_fn(status)

        return None

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state when real data arrives."""
        self._optimistic_state = None
        super()._handle_coordinator_update()    

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._optimistic_state = True
        self.async_write_ha_state()
        await self.entity_description.turn_on_fn(self.coordinator.api, self.device_id)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        self._optimistic_state = False
        self.async_write_ha_state()
        await self.entity_description.turn_off_fn(self.coordinator.api, self.device_id)
        await self.coordinator.async_request_refresh()
