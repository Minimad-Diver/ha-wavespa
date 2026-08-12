"""Test wavespa setup process."""

import asyncio
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wavespa import WavespaUpdateCoordinator
from custom_components.wavespa.wavespa.api import WavespaApi, WavespaApiResults
from custom_components.wavespa.wavespa.model import WavespaDevice, WavespaUserToken
from custom_components.wavespa.const import (
    CONF_API_ROOT,
    CONF_API_ROOT_EU,
    CONF_PASSWORD,
    CONF_UID,
    CONF_USER_TOKEN,
    CONF_USER_TOKEN_EXPIRY,
    CONF_USERNAME,
    DOMAIN,
)

_DEVICE = WavespaDevice(
    protocol_version=1,
    device_id="did",
    product_name="Wave Spa",
    alias="Spa",
    mcu_soft_version="1",
    mcu_hard_version="1",
    wifi_soft_version="1",
    wifi_hard_version="1",
    is_online=True,
)


async def test_setup_unload_and_reload_entry(hass: HomeAssistant, bypass_get_data):
    """Test entry setup and unload."""

    # This config entry has an auth token that expires far enough in
    # the future that no auth attempt should be made
    future = (datetime.now() + timedelta(days=31)).timestamp()
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_USER_TOKEN: "t0k3n",
            CONF_USER_TOKEN_EXPIRY: int(future),
            CONF_UID: "uid",
        },
        version=2,
        entry_id="test",
    )
    config_entry.add_to_hass(hass)

    # Set up the entry and assert that the values set during setup are where we expect
    # them to be. Because we have patched the WavespaUpdateCoordinator.async_get_data
    # call, no code from custom_components/wavespa/api.py actually runs.
    with patch(
        "custom_components.wavespa.wavespa.api.WavespaApi.get_user_token"
    ) as get_user_token_fn:
        await hass.config_entries.async_setup(config_entry.entry_id)

    assert DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]
    assert isinstance(
        hass.data[DOMAIN][config_entry.entry_id], WavespaUpdateCoordinator
    )

    # The token expires far enough in the future that a call to refresh
    # the token should not be made.
    get_user_token_fn.assert_not_called()

    # Reload the entry and assert that the data from above is still there
    await hass.config_entries.async_reload(config_entry.entry_id)
    assert DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]
    assert isinstance(
        hass.data[DOMAIN][config_entry.entry_id], WavespaUpdateCoordinator
    )

    # Unload the entry and verify that the data has been removed
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.entry_id not in hass.data[DOMAIN]


class _FakeWebSocket:
    """Stand-in for GizwitsWebSocket that records its own lifecycle."""

    def __init__(self, **kwargs: Any) -> None:
        self.started = False
        self.cancelled = False
        self.disconnected = False

    async def async_run(self) -> None:
        """Keep running until cancelled, like the real supervisor."""
        self.started = True
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def disconnect(self) -> None:
        """Record the graceful shutdown."""
        self.disconnected = True


async def test_websocket_task_is_cancelled_on_unload(hass: HomeAssistant):
    """Test the WebSocket supervisor doesn't outlive the config entry.

    The task used to be untracked, so a reload left the old client
    reconnecting forever alongside the new one.
    """
    future = (datetime.now() + timedelta(days=31)).timestamp()
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_USER_TOKEN: "t0k3n",
            CONF_USER_TOKEN_EXPIRY: int(future),
            CONF_UID: "uid",
        },
        version=2,
        entry_id="test",
    )
    config_entry.add_to_hass(hass)

    fake_ws = _FakeWebSocket()

    async def populate_devices(self: WavespaApi) -> None:
        """Give the entry a device, so the WebSocket client gets built."""
        self.devices = {"did": _DEVICE}

    async def fetch_cached(self: WavespaApi) -> WavespaApiResults:
        """Serve the empty state cache instead of calling the API."""
        return self.cached_results()

    with (
        patch.object(WavespaApi, "refresh_bindings", populate_devices),
        patch.object(WavespaApi, "fetch_data", fetch_cached),
        patch("custom_components.wavespa.GizwitsWebSocket", return_value=fake_ws),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)

        # Deliberately not async_block_till_done() - a background task is
        # exactly the thing that doesn't block it, and an untracked one would
        # hang here rather than failing an assertion.
        await asyncio.sleep(0.05)
        assert fake_ws.started

        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await asyncio.sleep(0.05)

    assert fake_ws.disconnected
    assert fake_ws.cancelled


async def test_polling_slows_only_once_websocket_connects(hass: HomeAssistant):
    """Test the slow polling interval is tied to the WebSocket actually connecting.

    Setup used to drop straight to 5-minute polling on the assumption the
    WebSocket would connect. A spa that never managed to connect was then left
    with neither pushes nor timely polling.
    """
    future = (datetime.now() + timedelta(days=31)).timestamp()
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_USER_TOKEN: "t0k3n",
            CONF_USER_TOKEN_EXPIRY: int(future),
            CONF_UID: "uid",
        },
        version=2,
        entry_id="test",
    )
    config_entry.add_to_hass(hass)

    fake_ws = _FakeWebSocket()

    async def populate_devices(self: WavespaApi) -> None:
        self.devices = {"did": _DEVICE}

    async def fetch_cached(self: WavespaApi) -> WavespaApiResults:
        return self.cached_results()

    with (
        patch.object(WavespaApi, "refresh_bindings", populate_devices),
        patch.object(WavespaApi, "fetch_data", fetch_cached),
        patch(
            "custom_components.wavespa.GizwitsWebSocket", return_value=fake_ws
        ) as ws_cls,
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)

        coordinator = hass.data[DOMAIN][config_entry.entry_id]

        # Nothing has connected yet, so polling is still the only live source
        assert coordinator.update_interval == timedelta(seconds=30)

        # The client is wired to tell the coordinator when that changes
        callbacks = ws_cls.call_args.kwargs
        connect_callback = callbacks["connect_callback"]
        assert connect_callback == coordinator.set_websocket_active

        connect_callback()
        assert coordinator.update_interval == timedelta(seconds=300)

        # A drop and recovery round trip, driven through the same wiring
        callbacks["disconnect_callback"]()
        assert coordinator.update_interval == timedelta(seconds=30)

        connect_callback()
        assert coordinator.update_interval == timedelta(seconds=300)

        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await asyncio.sleep(0.05)


async def test_setup_entry_expired_token(hass: HomeAssistant, bypass_get_data):
    """Test what happens when the auth token needs to be refreshed."""

    # This config entry has an auth token that needs renewal (<30 days)
    future = (datetime.now() + timedelta(days=15)).timestamp()
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_USER_TOKEN: "t0k3n",
            CONF_USER_TOKEN_EXPIRY: int(future),
        },
        version=2,
        entry_id="test",
    )
    config_entry.add_to_hass(hass)

    expected_token = WavespaUserToken(user_id="uid", user_token="new_token", expiry=123)

    with patch("custom_components.wavespa.wavespa.api.WavespaApi.get_user_token") as p:
        p.return_value = expected_token
        await hass.config_entries.async_setup(config_entry.entry_id)
        p.assert_called_once()

    updated_entry = hass.config_entries.async_get_entry(config_entry.entry_id)
    assert updated_entry is not None
    assert updated_entry.data[CONF_USER_TOKEN] == expected_token.user_token
    assert updated_entry.data[CONF_USER_TOKEN_EXPIRY] == expected_token.expiry


async def test_setup_entry_exception(hass: HomeAssistant, error_on_get_data):
    """Test ConfigEntryNotReady when API raises an exception during entry setup."""

    # This config entry has an auth token that expires in the future
    future = (datetime.now() + timedelta(days=31)).timestamp()
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_USER_TOKEN: "t0k3n",
            CONF_USER_TOKEN_EXPIRY: int(future),
        },
        version=2,
        entry_id="test",
    )

    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)

    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
