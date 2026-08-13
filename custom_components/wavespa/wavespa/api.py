"""Wavespa API."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
from logging import getLogger
from time import monotonic, time

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

# How long a control command suppresses polls for that device. A GET issued
# straight after a POST still returns the old values, so the local write is
# trusted for this long. Measured on the monotonic clock, so it is immune to
# host clock skew and to the API and host disagreeing about the time.
_LOCAL_WRITE_SETTLE_SECONDS = 15


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
        # A body that won't parse just means we fall through to the generic
        # raise below. The result is bound to None rather than left unset:
        # the previous version called raise_for_status() inside the handler
        # and then read api_error anyway, which was only safe because that
        # call always raises for a non-ok response.
        api_error: dict[str, Any] | None
        try:
            api_error = await response.json()
        except Exception:  # pylint: disable=broad-except
            api_error = None

        if api_error is not None:
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

        # Monotonic timestamp of the last local (control-command) write per
        # device, used to suppress polls that would report pre-command state.
        self._local_writes: dict[str, float] = {}

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
        self.devices = {
            device.device_id: device for device in await self._get_devices()
        }

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
        """Fetch the latest data for all devices.

        Device requests run concurrently. Issued in sequence, a slow first
        device ate into the budget available to the rest, so on an account with
        several spas the whole update could time out even though every
        individual request was within its own limit.
        """
        device_ids = list(self.devices)
        responses = await asyncio.gather(
            *(
                self._do_get(f"{self._api_root}/app/devdata/{did}/latest")
                for did in device_ids
            )
        )

        for did, latest_data in zip(device_ids, responses, strict=True):
            device_info = self.devices[did]

            # Get the age of the data according to the API
            api_update_timestamp = latest_data["updated_at"]

            # Zero indicates the device is offline
            # This has been observed after a device was offline for a few months
            if api_update_timestamp == 0:
                # In testing, the 'attrs' dictionary has been observed to be empty
                _LOGGER.debug("No data available for device %s", did)
                continue

            cached_state: WavespaDeviceStatus | None = self._state_cache.get(did)

            # A poll that raced a control command we just sent would report the
            # pre-command state, so a local write suppresses polls briefly.
            #
            # The window is measured on the local monotonic clock rather than by
            # comparing the API's updated_at against a locally stamped
            # timestamp. Those are two different clocks, and the old comparison
            # discarded every poll for as long as the host clock ran ahead of
            # the Gizwits server - indefinitely on a host with no working NTP,
            # which silently left the WebSocket as the only source of state.
            if self._local_write_is_recent(did):
                _LOGGER.debug(
                    "Ignoring poll for device %s; a local change is still settling",
                    did,
                )
                continue

            _LOGGER.debug("New data received for device %s", did)
            device_attrs = latest_data["attr"]

            # Merge onto any previously cached attrs. A poll response is
            # normally a full snapshot, but Time_filter has been observed to
            # be absent from some responses; merging preserves it (and any
            # other occasionally-missing field) from the last known state,
            # consistent with how WebSocket deltas are handled.
            if cached_state is not None:
                merged_attrs = {**cached_state.attrs, **device_attrs}
            else:
                merged_attrs = device_attrs

            self._state_cache[did] = WavespaDeviceStatus(
                latest_data["updated_at"], merged_attrs
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

    def cached_results(self) -> WavespaApiResults:
        """Return a snapshot of the cached state for every known device."""
        return WavespaApiResults(self._state_cache)

    def _local_write_is_recent(self, device_id: str) -> bool:
        """Return True if we changed this device within the settle window."""
        written_at = self._local_writes.get(device_id)
        if written_at is None:
            return False
        return (monotonic() - written_at) < _LOCAL_WRITE_SETTLE_SECONDS

    def merge_device_attrs(self, device_id: str, attrs: dict[str, Any]) -> None:
        """
        Merge a partial attribute delta into a device's cached state.

        Used both for WebSocket pushes, which only carry the fields that
        changed, and for the optimistic writes the ``spa_set_*`` methods make
        after a control POST. Unmentioned fields keep their last known value
        rather than vanishing.

        The cache entry is replaced rather than mutated in place, and is looked
        up fresh on every call, so callers must not hold a reference across an
        await and write to it afterwards - see ``_apply_control_result``.
        """
        existing = self._state_cache.get(device_id)
        merged_attrs = {**existing.attrs, **attrs} if existing else dict(attrs)
        self._state_cache[device_id] = WavespaDeviceStatus(
            timestamp=int(time()),
            attrs=merged_attrs,
        )

    def _require_known_device(self, device_id: str) -> None:
        """Raise unless we hold cached state for the given device."""
        if device_id not in self._state_cache:
            raise WavespaException(f"Device '{device_id}' is not recognised")

    def _apply_control_result(self, device_id: str, attrs: dict[str, Any]) -> None:
        """Record the effect of a control POST in the local state cache.

        Deliberately re-reads the cache instead of reusing the entry the caller
        looked up before awaiting the POST. A WebSocket delta arriving while
        that POST was in flight replaces the cached object via
        merge_device_attrs(), so writing to the pre-await reference would land
        on an orphan and silently drop the change we just made - most visibly
        for target temperature, which has no optimistic overlay in the UI to
        paper over it.

        Also records the write on the monotonic clock, so fetch_data can skip
        polls that would report the pre-command state without having to compare
        the API's clock against ours.
        """
        self.merge_device_attrs(device_id, attrs)
        self._local_writes[device_id] = monotonic()

    async def spa_set_filter(self, device_id: str, filtering: bool) -> None:
        """
        Turn the filter pump on/off on a spa device.

        Turning the filter pump off will also turn off the heater, which cannot
        run without it. The bubbles are independent and are left alone.
        """
        self._require_known_device(device_id)

        api_value = 1 if filtering else 0
        _LOGGER.debug("Setting filter mode to %s", "ON" if filtering else "OFF")
        await self._do_control_post(device_id, Filter=api_value)

        updates: dict[str, Any] = {"Filter": api_value}
        if not filtering:
            updates["Heater"] = 0
        self._apply_control_result(device_id, updates)

    async def spa_set_heat(self, device_id: str, heat: bool) -> None:
        """
        Turn the heater on/off on a spa device.

        Turning the heater on will also turn on the filter pump.
        """
        self._require_known_device(device_id)

        api_value = 1 if heat else 0
        _LOGGER.debug("Setting heater mode to %s", "ON" if heat else "OFF")
        await self._do_control_post(device_id, Heater=api_value)

        updates: dict[str, Any] = {"Heater": api_value}
        if heat:
            updates["Filter"] = 1
        self._apply_control_result(device_id, updates)

    async def spa_set_target_temp(self, device_id: str, target_temp: int) -> None:
        """Set the target temperature on a spa device."""
        self._require_known_device(device_id)

        target_temp = int(target_temp)
        _LOGGER.debug("Setting target temperature to %d", target_temp)
        await self._do_control_post(device_id, Temperature_setup=target_temp)

        self._apply_control_result(device_id, {"Temperature_setup": target_temp})

    async def spa_set_bubbles(self, device_id: str, bubbles: bool) -> None:
        """
        Turn the bubbles on/off on a spa device.

        The bubbles are independent of the heater and filter pump, so no other
        cached attribute is changed.
        """
        self._require_known_device(device_id)

        api_value = 1 if bubbles else 0
        _LOGGER.debug("Setting bubbles mode to %s", "ON" if bubbles else "OFF")
        await self._do_control_post(device_id, Bubble=api_value)

        self._apply_control_result(device_id, {"Bubble": api_value})

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
