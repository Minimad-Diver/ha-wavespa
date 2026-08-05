"""Switch platform support."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

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


_SPA_POWER_SWITCH = WavespaSwitchEntityDescription(
    key="Heater",
    name="Heater",
    icon=Icon.POWER,
    value_fn=lambda s: bool(s.attrs["Heater"]),
    turn_on_fn=lambda api, device_id: api.spa_set_power(device_id, True),
    turn_off_fn=lambda api, device_id: api.spa_set_power(device_id, False),
)

_SPA_FILTER_SWITCH = WavespaSwitchEntityDescription(
    key="Filter",
    name="Filter",
    icon=Icon.FILTER,
    value_fn=lambda s: bool(s.attrs["Filter"]),
    turn_on_fn=lambda api, device_id: api.spa_set_filter(device_id, True),
    turn_off_fn=lambda api, device_id: api.spa_set_filter(device_id, False),
)

_SPA_BUBBLES_SWITCH = WavespaSwitchEntityDescription(
    key="Bubble",
    name="Bubbles",
    icon=Icon.BUBBLES,
    value_fn=lambda s: bool(s.attrs["Bubble"]),
    turn_on_fn=lambda api, device_id: api.spa_set_bubbles(device_id, True),
    turn_off_fn=lambda api, device_id: api.spa_set_bubbles(device_id, False),
)

_SPA_LOCK_SWITCH = WavespaSwitchEntityDescription(
    key="spa_locked",
    name="Spa Locked",
    icon=Icon.LOCK,
    value_fn=lambda s: bool(s.attrs["locked"]),
    turn_on_fn=lambda api, device_id: api.spa_set_locked(device_id, True),
    turn_off_fn=lambda api, device_id: api.spa_set_locked(device_id, False),
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
                        _SPA_POWER_SWITCH
                    ),
                    WavespaSwitch(
                        coordinator,
                        config_entry,
                        device_id,
                        _SPA_FILTER_SWITCH,
                    ),
                    WavespaSwitch(
                        coordinator,
                        config_entry,
                        device_id,
                        _SPA_BUBBLES_SWITCH,
                    ),
                    WavespaSwitch(
                        coordinator,
                        config_entry,
                        device_id,
                        _SPA_LOCK_SWITCH,
                    ),
                ]
            )

    async_add_entities(entities)


class WavespaSwitch(WavespaEntity, SwitchEntity):
    """Wavespa switch entity."""

    entity_description: WavespaSwitchEntityDescription
    _attr_assumed_state = True

    # If the device hasn't confirmed an optimistic change within this many
    # seconds (e.g. the command silently failed), fall back to whatever the
    # coordinator's real data says rather than getting stuck on a stale
    # optimistic value forever.
    _OPTIMISTIC_TIMEOUT_SECONDS = 10

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
        self._optimistic_state_set_at: float | None = None

    def _set_optimistic(self, value: bool) -> None:
        """Set the optimistic value and stamp it for the timeout check."""
        self._optimistic_state = value
        self._optimistic_state_set_at = monotonic()

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        if status := self.status:
            return self.entity_description.value_fn(status)

        return None

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state once confirmed, or after a timeout.

        A coordinator update (poll or WebSocket push) can arrive before the
        physical spa has actually applied a change we just sent - clearing
        the optimistic overlay unconditionally would make the switch briefly
        flash back to its old state in that window. So we only clear it
        once the confirmed status agrees with what we optimistically set,
        keeping the UI consistent with what the person just did until the
        device genuinely catches up.

        However, if the command never takes effect (e.g. it silently fails),
        that condition would never be true and the switch would be stuck on
        the optimistic value forever. To avoid that, we also give up on the
        optimistic value after _OPTIMISTIC_TIMEOUT_SECONDS, falling back to
        whatever the real device data says.
        """
        if self._optimistic_state is not None and self.status is not None:
            try:
                actual = self.entity_description.value_fn(self.status)
            except (KeyError, TypeError):
                # A partial/malformed status shouldn't crash the update -
                # just treat it as unconfirmed and let the timeout decide.
                actual = None
            confirmed = actual == self._optimistic_state
            timed_out = (
                self._optimistic_state_set_at is not None
                and monotonic() - self._optimistic_state_set_at
                >= self._OPTIMISTIC_TIMEOUT_SECONDS
            )
            if confirmed or timed_out:
                self._optimistic_state = None
                self._optimistic_state_set_at = None

        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._set_optimistic(True)
        self.async_write_ha_state()
        await self.entity_description.turn_on_fn(self.coordinator.api, self.device_id)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        self._set_optimistic(False)
        self.async_write_ha_state()
        await self.entity_description.turn_off_fn(self.coordinator.api, self.device_id)
        await self.coordinator.async_request_refresh()
