"""Switch platform support."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import WavespaConfigEntry, WavespaUpdateCoordinator
from .wavespa.api import WavespaApi, WavespaException
from .wavespa.model import WavespaDeviceStatus, WavespaDeviceType
from .entity import WavespaEntity


@dataclass(frozen=True, kw_only=True)
class WavespaSwitchEntityDescription(SwitchEntityDescription):
    """Entity description for switches."""

    value_fn: Callable[[WavespaDeviceStatus], bool | None]
    turn_on_fn: Callable[[WavespaApi, str], Awaitable[None]]
    turn_off_fn: Callable[[WavespaApi, str], Awaitable[None]]


_SPA_FILTER_SWITCH = WavespaSwitchEntityDescription(
    key="Filter",
    translation_key="filter",
    value_fn=lambda s: s.flag("Filter"),
    turn_on_fn=lambda api, device_id: api.spa_set_filter(device_id, True),
    turn_off_fn=lambda api, device_id: api.spa_set_filter(device_id, False),
)

_SPA_BUBBLES_SWITCH = WavespaSwitchEntityDescription(
    key="Bubble",
    translation_key="bubbles",
    value_fn=lambda s: s.flag("Bubble"),
    turn_on_fn=lambda api, device_id: api.spa_set_bubbles(device_id, True),
    turn_off_fn=lambda api, device_id: api.spa_set_bubbles(device_id, False),
)


# Entity state comes from the coordinator, so updates are not per-entity
# polling and do not need serialising.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: WavespaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switch entities."""
    coordinator = config_entry.runtime_data

    entities: list[WavespaEntity] = []

    for device_id, device in coordinator.api.devices.items():
        if device.device_type in [
            WavespaDeviceType.WAVESPA_EU,
            WavespaDeviceType.WAVESPA_US,
        ]:
            entities.extend(
                [
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
        config_entry: WavespaConfigEntry,
        device_id: str,
        description: WavespaSwitchEntityDescription,
    ) -> None:
        """Initialize switch."""
        super().__init__(coordinator, config_entry, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._optimistic_state: bool | None = None
        self._cancel_optimistic_expiry: CALLBACK_TYPE | None = None

    def _set_optimistic(self, value: bool) -> None:
        """Show `value` immediately, and arm the expiry that gives up on it.

        The expiry is a real timer rather than a timestamp checked during
        coordinator updates. Checking on update meant the deadline only came
        round when an update happened to arrive, and with the WebSocket
        connected that is every five minutes - so a silently failed command
        could show the wrong state for far longer than the ten seconds this
        constant claims.
        """
        self._optimistic_state = value
        self._cancel_optimistic_timer()
        self._cancel_optimistic_expiry = async_call_later(
            self.hass,
            self._OPTIMISTIC_TIMEOUT_SECONDS,
            self._expire_optimistic,
        )

    def _cancel_optimistic_timer(self) -> None:
        """Cancel any armed expiry."""
        if self._cancel_optimistic_expiry is not None:
            self._cancel_optimistic_expiry()
            self._cancel_optimistic_expiry = None

    def _clear_optimistic(self) -> None:
        """Drop the optimistic value and disarm its expiry."""
        self._optimistic_state = None
        self._cancel_optimistic_timer()

    @callback
    def _expire_optimistic(self, _now: datetime) -> None:
        """Give up on an unconfirmed optimistic value and show reality."""
        self._cancel_optimistic_expiry = None
        if self._optimistic_state is None:
            return
        self._optimistic_state = None
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Disarm the expiry so it can't fire after the entity is gone."""
        self._cancel_optimistic_timer()
        await super().async_will_remove_from_hass()

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        if status := self.status:
            try:
                return self.entity_description.value_fn(status)
            except (KeyError, TypeError):
                # The expected attribute isn't present in this status
                # (e.g. a partial update or a model that doesn't report it).
                # Report unknown rather than raising and breaking the entity.
                return None

        return None

    def _handle_coordinator_update(self) -> None:
        """Clear the optimistic value once the device confirms it.

        A coordinator update (poll or WebSocket push) can arrive before the
        physical spa has actually applied a change we just sent - clearing
        the optimistic overlay unconditionally would make the switch briefly
        flash back to its old state in that window. So we only clear it
        once the confirmed status agrees with what we optimistically set,
        keeping the UI consistent with what the person just did until the
        device genuinely catches up.

        A command that never takes effect is handled by the expiry timer armed
        in _set_optimistic, not here - waiting for an update to notice would
        make the deadline depend on the polling interval.
        """
        if self._optimistic_state is not None and self.status is not None:
            try:
                actual = self.entity_description.value_fn(self.status)
            except (KeyError, TypeError):
                # A partial/malformed status shouldn't crash the update -
                # just treat it as unconfirmed and let the expiry decide.
                actual = None
            if actual == self._optimistic_state:
                self._clear_optimistic()

        super()._handle_coordinator_update()

    async def _async_set(self, value: bool) -> None:
        """Apply a new state optimistically, then send it to the spa."""
        self._set_optimistic(value)
        self.async_write_ha_state()

        description = self.entity_description
        action = description.turn_on_fn if value else description.turn_off_fn
        try:
            await action(self.coordinator.api, self.device_id)
        except WavespaException as ex:
            # Drop the optimistic value straight away rather than leaving the
            # switch showing a change that demonstrably did not happen, and
            # report it as a HomeAssistantError so the UI shows a message
            # instead of an unhandled traceback.
            self._clear_optimistic()
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Failed to turn {'on' if value else 'off'} {self.name}: {ex}"
            ) from ex

        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._async_set(False)
