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

from .wavespa.api import WavespaApi, WavespaApiResults, WavespaAuthException
from .wavespa.websocket import GizwitsWebSocket

_LOGGER = getLogger(__name__)

# The config entry carries the coordinator as its runtime data.
type WavespaConfigEntry = ConfigEntry["WavespaUpdateCoordinator"]

# How often to poll the cloud API when it's the only source of state, and the
# slower rate used once the WebSocket is delivering pushes and polling is just
# a safety net.
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
        self._ws_last_update: dict[str, float] = {}  # Track WebSocket update times
        # Set by async_setup_entry once the client is built; None when no
        # UID is stored or the account has no devices to subscribe to.
        self.websocket: GizwitsWebSocket | None = None

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

        Increases polling frequency to 30 seconds as fallback when
        WebSocket connection is lost. This ensures the integration
        continues functioning reliably even without real-time updates.
        """
        if self.update_interval == _POLL_INTERVAL:
            return

        _LOGGER.warning("WebSocket disconnected, reverting to 30-second polling")
        self.update_interval = _POLL_INTERVAL

    def set_websocket_active(self) -> None:
        """Set polling interval for WebSocket-active mode.

        Reduces polling frequency to 5 minutes when WebSocket is providing
        real-time updates. Polling continues as a safety net to catch any
        missed updates or handle WebSocket connection issues.

        Called on every successful connection, not just the first, so that
        polling drops back down again after the feed recovers from an outage.
        A flapping connection would otherwise log on every attempt, so the
        message is only emitted when the interval actually changes.
        """
        if self.update_interval == _WEBSOCKET_POLL_INTERVAL:
            return

        _LOGGER.info("WebSocket active, reducing polling to 5-minute intervals")
        self.update_interval = _WEBSOCKET_POLL_INTERVAL
