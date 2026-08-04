"""Wavespa API."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
from logging import getLogger
from time import time

from typing import Any

from aiohttp import ClientResponse, ClientSession

from ..const import GIZWITS_APP_ID
from .model import (
    WavespaDevice,
    WavespaDeviceStatus,
    WavespaDeviceType,
    WavespaUserToken,
)

_LOGGER = getLogger(__name__)
_HEADERS = {
    "Content-type": "application/json; charset=UTF-8",
    "X-Gizwits-Application-Id": GIZWITS_APP_ID,
    "User-Agent": "okhttp/5.0.0-alpha.3",
    "Connection": "Keep-Alive",
}
_TIMEOUT = 10


@dataclass
class WavespaApiResults:
    """A snapshot of device status reports returned from the API."""

    devices: dict[str, WavespaDeviceStatus]


class WavespaException(Exception):
    """An exception while using the API."""


class WavespaOfflineException(WavespaException):
    """Device is offline."""

    def __init__(self) -> None:
        """Construct the exception."""
        super().__init__("Server reports device is offline")


class WavespaAuthException(WavespaException):
    """An authentication error."""


class WavespaTokenInvalidException(WavespaAuthException):
    """Auth token is invalid or expired."""

    def __init__(self) -> None:
        super().__init__("Server reports auth token is invalid or expired")


class WavespaUserDoesNotExistException(WavespaAuthException):
    """User does not exist."""

    def __init__(self) -> None:
        super().__init__("Server reports user does not exist")


class WavespaIncorrectPasswordException(WavespaAuthException):
    """Password is incorrect."""

    def __init__(self) -> None:
        super().__init__("Server reports password is incorrect")


async def _raise_for_status(response: ClientResponse) -> None:
    """Raise an exception based on the response."""
    if response.ok:
        return

    # The API often provides useful error descriptions in JSON format
    if response.content_type == "application/json":
        try:
            api_error = await response.json()
        except Exception:  # pylint: disable=broad-except
            response.raise_for_status()

        error_code = api_error.get("error_code", 0)
        if error_code == 9004:
            raise WavespaTokenInvalidException()
        if error_code == 9005:
            raise WavespaUserDoesNotExistException()
        if error_code == 9042:
            raise WavespaOfflineException()
        if error_code == 9020:
            raise WavespaIncorrectPasswordException()

    # If we can't pull out a Wavespa error code, provide more detail for debugging
    response.raise_for_status()


class WavespaApi:
    """Wavespa API."""

    def __init__(self, session: ClientSession, user_token: str, api_root: str) -> None:
        """Initialize the API with a user token."""
        self._session = session
        self._user_token = user_token
        self._api_root = api_root

        # Maps device IDs to device info
        self.devices: dict[str, WavespaDevice] = {}

        # Cache containing state information for each device received from the API
        # This is used to work around an annoyance where changes to settings via
        # a POST request are not immediately reflected in a subsequent GET request.
        #
        # When updating state via HA, we update the cache and return this value
        # until the API can provide us with a response containing a timestamp
        # more recent than the local update.
        self._state_cache: dict[str, WavespaDeviceStatus] = {}

    @staticmethod
    async def get_user_token(
        session: ClientSession, username: str, password: str, api_root: str
    ) -> WavespaUserToken:
        """
        Login and obtain a user token.

        The server rate-limits requests for this fairly aggressively.
        """
        body = {"username": username, "password": password, "lang": "en"}

        async with asyncio.timeout(_TIMEOUT):
            response = await session.post(
                f"{api_root}/app/login", headers=_HEADERS, json=body
            )
            await _raise_for_status(response)
            api_data = await response.json()

        return WavespaUserToken(
            api_data["uid"], api_data["token"], api_data["expire_at"]
        )

    async def refresh_bindings(self) -> None:
        """Refresh and store the list of devices available in the account."""
        previous_devices = self.devices

        new_devices = {
            device.device_id: device for device in await self._get_devices()
        }

        # `time_filter` is tracked locally (it isn't part of the bindings API
        # response) and lives on the WavespaDevice object itself. Since this
        # method rebuilds that object from scratch every time it's called
        # (once per poll cycle), carry the previous value forward onto the
        # new object before it replaces the old one. This closes a race
        # condition where a WebSocket update landing between this refresh
        # and the next fetch_data() call would otherwise see a blank
        # (None) time_filter and briefly report the filter sensor as
        # unknown.
        for did, device in new_devices.items():
            if (previous_device := previous_devices.get(did)) is not None:
                device.time_filter = previous_device.time_filter

        self.devices = new_devices

    async def _get_devices(self) -> list[WavespaDevice]:
        """Get the list of devices available in the account."""
        api_data = await self._do_get(f"{self._api_root}/app/bindings")

        sanitized_data = self._sanitize_bindings_response(api_data)
        _LOGGER.debug("Device list refreshed: %s", json.dumps(sanitized_data))

        return [
            WavespaDevice(
                raw["protoc"],
                raw["did"],
                raw["product_name"],
                raw["dev_alias"],
                raw["mcu_soft_version"],
                raw["mcu_hard_version"],
                raw["wifi_soft_version"],
                raw["wifi_hard_version"],
                raw["is_online"],
                ws_host=raw.get("host", "m2m.gizwits.com"),
                ws_port=raw.get("wss_port", 8880),
            )
            for raw in api_data["devices"]
        ]

    async def fetch_data(self) -> WavespaApiResults:
        """Fetch the latest data for all devices."""
        for did, device_info in self.devices.items():
            latest_data = await self._do_get(
                f"{self._api_root}/app/devdata/{did}/latest"
            )

            # Get the age of the data according to the API
            api_update_timestamp = latest_data["updated_at"]

            # Zero indicates the device is offline
            # This has been observed after a device was offline for a few months
            if api_update_timestamp == 0:
                # In testing, the 'attrs' dictionary has been observed to be empty
                _LOGGER.debug("No data available for device %s", did)
                continue

            # Work out whether the received API update is more recent than the
            # locally cached state
            local_update_timestamp = 0
            cached_state: WavespaDeviceStatus | None
            if cached_state := self._state_cache.get(did):
                local_update_timestamp = cached_state.timestamp

            # If the API timestamp is more recent, update the cache
            if api_update_timestamp < local_update_timestamp:
                _LOGGER.debug(
                    "Ignoring update for device %s as local data is newer", did
                )
                continue

            _LOGGER.debug("New data received for device %s", did)
            device_attrs = latest_data["attr"]

            # Preserve the previous time_filter value if it exists
            previous_time_filter = None
            if cached_state is not None:
                previous_time_filter = cached_state.time_filter

            self._state_cache[did] = WavespaDeviceStatus(
                latest_data["updated_at"], device_attrs, device_info
            )

            # Update the cached state with the latest data
            # If Time_filter is in the response, use it; otherwise preserve the previous value
            if (
                "Time_filter" in device_attrs
                and device_attrs["Time_filter"] is not None
            ):
                self._state_cache[did].time_filter = device_attrs["Time_filter"]
            elif previous_time_filter is not None:
                # Preserve the previous value if Time_filter is not in this API response
                self._state_cache[did].time_filter = previous_time_filter
                _LOGGER.debug(
                    "Time_filter not in API response for device %s, preserving previous value: %d",
                    did,
                    previous_time_filter,
                )

            attr_dump = json.dumps(device_attrs)

            if device_info.device_type == WavespaDeviceType.UNKNOWN:
                _LOGGER.warning(
                    "Status for unknown device type '%s' returned: %s",
                    device_info.product_name,
                    attr_dump,
                )
            else:
                _LOGGER.debug(
                    "Status for device type '%s' returned: %s",
                    device_info.product_name,
                    attr_dump,
                )

        return WavespaApiResults(self._state_cache)

    async def airjet_spa_set_power(self, device_id: str, power: bool) -> None:
        """Turn the spa on/off."""
        if (cached_state := self._state_cache.get(device_id)) is None:
            raise WavespaException(f"Device '{device_id}' is not recognised")

        api_value = 1 if power else 0
        _LOGGER.debug("Setting power to %s", "ON" if power else "OFF")
        await self._do_control_post(device_id, Heater=api_value)
        cached_state.timestamp = int(time())
        cached_state.attrs["Heater"] = api_value
        if not power:
            # When powering off, all other functions also turn off
            cached_state.attrs["Filter"] = 0
            cached_state.attrs["Heater"] = 0
            cached_state.attrs["Bubble"] = 0

    async def airjet_spa_set_filter(self, device_id: str, filtering: bool) -> None:
        """Turn the filter pump on/off on a spa device."""
        if (cached_state := self._state_cache.get(device_id)) is None:
            raise WavespaException(f"Device '{device_id}' is not recognised")

        api_value = 1 if filtering else 0
        _LOGGER.debug("Setting filter mode to %s", "ON" if filtering else "OFF")
        await self._do_control_post(device_id, Filter=api_value)
        cached_state.timestamp = int(time())
        cached_state.attrs["Filter"] = api_value
        if filtering:
            cached_state.attrs["Filter"] = 1
        else:
            cached_state.attrs["Bubble"] = 0
            cached_state.attrs["Heater"] = 0

    async def airjet_spa_set_heat(self, device_id: str, heat: bool) -> None:
        """
        Turn the heater on/off on a spa device.

        Turning the heater on will also turn on the filter pump.
        """
        if (cached_state := self._state_cache.get(device_id)) is None:
            raise WavespaException(f"Device '{device_id}' is not recognised")

        api_value = 1 if heat else 0
        _LOGGER.debug("Setting heater mode to %s", "ON" if heat else "OFF")
        await self._do_control_post(device_id, Heater=api_value)
        cached_state.timestamp = int(time())
        cached_state.attrs["Heater"] = api_value
        if heat:
            cached_state.attrs["Filter"] = 1

    async def airjet_spa_set_target_temp(
        self, device_id: str, target_temp: int
    ) -> None:
        """Set the target temperature on a spa device."""
        if (cached_state := self._state_cache.get(device_id)) is None:
            raise WavespaException(f"Device '{device_id}' is not recognised")

        target_temp = int(target_temp)
        _LOGGER.debug("Setting target temperature to %d", target_temp)
        await self._do_control_post(device_id, Temperature_setup=target_temp)
        cached_state.timestamp = int(time())
        cached_state.attrs["Temperature_setup"] = target_temp

    async def airjet_spa_set_locked(self, device_id: str, locked: bool) -> None:
        """Lock or unlock the physical control panel on a spa device."""
        if (cached_state := self._state_cache.get(device_id)) is None:
            raise WavespaException(f"Device '{device_id}' is not recognised")

        api_value = 1 if locked else 0
        _LOGGER.debug("Setting lock state to %s", "ON" if locked else "OFF")
        await self._do_control_post(device_id, locked=api_value)
        cached_state.timestamp = int(time())
        cached_state.attrs["locked"] = api_value

    async def airjet_spa_set_bubbles(self, device_id: str, bubbles: bool) -> None:
        """Turn the bubbles on/off on an Airjet spa device."""
        if (cached_state := self._state_cache.get(device_id)) is None:
            raise WavespaException(f"Device '{device_id}' is not recognised")

        _LOGGER.debug("Setting bubbles mode to %s", "ON" if bubbles else "OFF")
        await self._do_control_post(device_id, Bubble=1 if bubbles else 0)
        cached_state.timestamp = int(time())
        cached_state.attrs["Bubble"] = bubbles
        if bubbles:
            cached_state.attrs["Heater"] = 1

    async def _do_get(self, url: str) -> dict[str, Any]:
        """Make an API call to the specified URL, returning the response as a JSON object."""
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        async with asyncio.timeout(_TIMEOUT):
            response = await self._session.get(url, headers=headers)
            await _raise_for_status(response)

            # All API responses are encoded using JSON, however the headers often incorrectly
            # state 'text/html' as the content type.
            # We have to disable the check to avoid an exception.
            response_json: dict[str, Any] = await response.json(content_type=None)
            return response_json

    async def _do_control_post(
        self, device_id: str, **kwargs: int | str
    ) -> dict[str, Any]:
        return await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            {"attrs": kwargs},
        )

    async def _do_post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """Make an API call to the specified URL, returning the response as a JSON object."""
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        async with asyncio.timeout(_TIMEOUT):
            response = await self._session.post(url, headers=headers, json=body)
            await _raise_for_status(response)

            # All API responses are encoded using JSON, however the headers often incorrectly
            # state 'text/html' as the content type.
            # We have to disable the check to avoid an exception.
            response_json: dict[str, Any] = await response.json(content_type=None)
            return response_json

    @staticmethod
    def _sanitize_bindings_response(bindings: dict[str, Any]) -> dict[str, Any]:
        """Remove potentially sensitive data from device listings for logging purposes.

        People have a habit of simply copying & pasting to online communities without
        considering whether any of that information could be abused.
        """

        # Do all this in a safe way in case the response isn't as expected
        # At least we'll get log output we can work with
        sanitized = deepcopy(bindings)
        for device in sanitized.get("devices", {}):
            if (did := device.get("did")) is not None:
                device["did"] = "*" * len(did)
            if (mac := device.get("passcode")) is not None:
                device["passcode"] = "*" * len(mac)
            if (mac := device.get("product_key")) is not None:
                device["product_key"] = "*" * len(mac)
            if (mac := device.get("mac")) is not None:
                device["mac"] = "*" * len(mac)

        return sanitized
