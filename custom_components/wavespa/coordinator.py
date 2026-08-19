"""Data update coordinator for the Wavespa API."""

import asyncio
from datetime import timedelta
from logging import getLogger
from time import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .lan import GizwitsLanSession
from .wavespa.api import WavespaApi, WavespaApiResults, WavespaAuthException
from .wavespa.websocket import GizwitsWebSocket

_LOGGER = getLogger(__name__)

# The config entry carries the coordinator as its runtime data.
type WavespaConfigEntry = ConfigEntry["WavespaUpdateCoordinator"]

# How often to poll the cloud API when it's the only source of state, and the
# slower rate used once a push transport - the WebSocket, the LAN, or both - is
# delivering updates and polling is just a safety net.
_POLL_INTERVAL = timedelta(seconds=30)
_WEBSOCKET_POLL_INTERVAL = timedelta(seconds=300)


class WavespaUpdateCoordinator(DataUpdateCoordinator[WavespaApiResults]):
    """Update coordinator that polls the device status for all devices in an account."""

    def __init__(
        self, hass: HomeAssistant, config_entry: WavespaConfigEntry, api: WavespaApi
    ) -> None:
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Wavespa API",
            update_interval=_POLL_INTERVAL,
        )
        self.api = api
        # Wall-clock time of the last WebSocket push per device, exposed
        # through last_websocket_update() for diagnostics. Answers "is the
        # real-time feed actually delivering?", which the socket being
        # connected does not.
        self._ws_last_update: dict[str, float] = {}
        # Set by async_setup_entry once the client is built; None when no
        # UID is stored or the account has no devices to subscribe to.
        self.websocket: GizwitsWebSocket | None = None
        # Set by async_setup_entry when a LAN host is configured; None when
        # local control is off, which is the default.
        self.lan: GizwitsLanSession | None = None
        # Wall-clock time of the last LAN push, and which device it belongs
        # to. A single session serves one spa - see async_setup_entry for why
        # a multi-device account does not get one.
        self._lan_device_id: str | None = None
        self._lan_last_update: float | None = None
        # Which push transports are currently delivering. Polling slows down
        # while either is live and speeds back up only when both have stopped,
        # so losing one transport does not undo the other's saving.
        self._ws_active = False
        self._lan_active = False

    ## fix from https://github.com/cdpuk/ha-bestway/issues/86
    async def _async_update_data(self) -> WavespaApiResults:
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            async with asyncio.timeout(10):
                try:
                    await self.api.refresh_bindings()
                except WavespaAuthException:
                    # An expired or revoked token won't fix itself, so this
                    # one isn't swallowed with the rest.
                    raise
                except Exception as ex:  # pylint: disable=broad-except
                    # A failed device-list refresh shouldn't block the status
                    # fetch below, which can still serve the known devices.
                    _LOGGER.warning("Failed to refresh device list: %s", ex)

                return await self.api.fetch_data()
        except WavespaAuthException as ex:
            # Raising this is what starts HA's reauth flow. Letting it escape
            # as a generic error would become UpdateFailed instead, leaving
            # the user with permanently unavailable entities and no prompt.
            raise ConfigEntryAuthFailed from ex

    def handle_websocket_update(self, device_id: str, attrs: dict[str, Any]) -> None:
        """Handle real-time device update from WebSocket.

        Updates the device state cache with real-time data from WebSocket
        and triggers immediate entity updates. This provides sub-second
        update latency compared to 30-second polling.

        Args:
            device_id: Device ID (DID) that was updated
            attrs: Device attributes from WebSocket s2c_noti message
        """
        _LOGGER.debug(
            "WebSocket update for device %s with %d attributes", device_id, len(attrs)
        )

        if device_id not in self.api.devices:
            _LOGGER.warning(
                "Received WebSocket update for unrecognised device %s", device_id
            )
            return

        # A WebSocket s2c_noti delta only contains the fields that changed,
        # not the full device state. Merging it onto the existing cached attrs
        # keeps unmentioned fields (e.g. Time_filter, or any control field an
        # entity reads) at their last known value instead of vanishing and
        # causing KeyErrors or dropped readings.
        self.api.merge_device_attrs(device_id, attrs)

        # Track last WebSocket update time for this device
        self._ws_last_update[device_id] = time()

        # Trigger immediate entity updates
        self.async_set_updated_data(self.api.cached_results())

    def last_websocket_update(self, device_id: str) -> float | None:
        """Return when this device last sent a push, or None if it never has.

        None is meaningful: it distinguishes a spa that has sent nothing since
        setup from one whose feed has merely gone quiet, which is the first
        thing worth knowing when the WebSocket looks connected but the state
        is stale.
        """
        return self._ws_last_update.get(device_id)

    def handle_websocket_online_status(self, device_id: str, is_online: bool) -> None:
        """Apply a device online/offline notification from the WebSocket.

        Without this the flag only moved when a bindings refresh happened to
        notice, so a spa could show as connected for up to a polling interval
        after it had actually dropped.
        """
        if device_id not in self.api.devices:
            _LOGGER.debug(
                "Ignoring online status for unrecognised device %s", device_id
            )
            return

        if not self.api.set_device_online(device_id, is_online):
            return

        _LOGGER.debug(
            "Device %s reported %s", device_id, "online" if is_online else "offline"
        )
        self.async_set_updated_data(self.api.cached_results())

    def handle_websocket_disconnect(self) -> None:
        """Handle WebSocket disconnection.

        Returns to 30-second polling unless another push transport is still
        delivering, in which case the slower rate is still justified.
        """
        self._ws_active = False
        self._apply_poll_interval()

    def set_websocket_active(self) -> None:
        """Set polling interval for WebSocket-active mode.

        Called on every successful connection, not just the first, so that
        polling drops back down again after the feed recovers from an outage.
        """
        self._ws_active = True
        self._apply_poll_interval()

    def set_lan_active(self) -> None:
        """Record that the LAN session is connected and delivering."""
        self._lan_active = True
        self._apply_poll_interval()

    def handle_lan_disconnect(self) -> None:
        """Record that the LAN session has dropped."""
        self._lan_active = False
        self._apply_poll_interval()

    def _apply_poll_interval(self) -> None:
        """Pick the polling rate from which push transports are delivering.

        Polling is the safety net, so it only slows down while something is
        pushing and speeds back up once nothing is. Tracking the transports
        separately matters: with a single interval flag, the LAN dropping
        would have undone the WebSocket's saving and left a perfectly healthy
        push feed polling every 30 seconds.

        A flapping connection calls this repeatedly, so the message is emitted
        only when the interval actually changes.
        """
        wanted = (
            _WEBSOCKET_POLL_INTERVAL
            if (self._ws_active or self._lan_active)
            else _POLL_INTERVAL
        )
        if self.update_interval == wanted:
            return

        if wanted == _POLL_INTERVAL:
            _LOGGER.warning("No push transport connected, reverting to 30s polling")
        else:
            live = ", ".join(
                name
                for name, active in (
                    ("WebSocket", self._ws_active),
                    ("LAN", self._lan_active),
                )
                if active
            )
            _LOGGER.info("%s active, reducing polling to 5-minute intervals", live)

        self.update_interval = wanted

    def handle_lan_update(self, attrs: dict[str, Any]) -> None:
        """Apply a status decoded from the local network.

        The LAN sends a full status rather than a delta, but this still merges
        rather than replaces, so a field the product does not report keeps its
        last known value exactly as it does for a WebSocket delta.

        Identical frames are dropped. A single state change was measured
        arriving as a burst of three frames within 2.5 seconds - one of them
        byte-identical to the one before it - so without this every toggle
        would wake every entity two or three times over.
        """
        device_id = self._lan_device_id
        if device_id is None or device_id not in self.api.devices:
            _LOGGER.warning("Discarding a LAN update with no device to apply it to")
            return

        self._lan_last_update = time()

        cached = self.data.devices.get(device_id) if self.data else None
        if cached is not None and all(
            cached.attrs.get(key) == value for key, value in attrs.items()
        ):
            _LOGGER.debug("Ignoring a LAN status identical to the cached state")
            return

        _LOGGER.debug(
            "LAN update for device %s with %d attributes", device_id, len(attrs)
        )
        self.api.merge_device_attrs(device_id, attrs)
        self.async_set_updated_data(self.api.cached_results())

    def has_live_push(self, device_id: str) -> bool:
        """Whether a push transport serving this device is currently connected.

        Used for entity availability. A connected transport means we are in
        touch with the spa right now, which is at least as good evidence as a
        successful cloud poll - a LAN session proves it directly, since it
        pings every few seconds and the device drops the connection if we
        stop.

        The WebSocket is account-wide, so it counts for every device. The LAN
        session speaks for exactly one spa, so it counts only for that one -
        otherwise a second spa would be reported as reachable on the strength
        of the first one's connection.
        """
        if self.websocket is not None and self.websocket.is_connected:
            return True
        return (
            self.lan is not None
            and self.lan.is_connected
            and device_id == self._lan_device_id
        )

    def last_lan_update(self, device_id: str) -> float | None:
        """Return when the LAN last delivered a status for this device."""
        if device_id != self._lan_device_id:
            return None
        return self._lan_last_update

    def set_lan_device(self, device_id: str) -> None:
        """Name the device the LAN session speaks for."""
        self._lan_device_id = device_id
