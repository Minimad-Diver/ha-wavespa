"""Integration tests for WebSocket coordinator callbacks."""

from datetime import timedelta
from time import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wavespa.wavespa.api import WavespaApi
from custom_components.wavespa.wavespa.model import WavespaDeviceStatus
from custom_components.wavespa.const import CONF_API_ROOT, CONF_API_ROOT_EU, DOMAIN
from custom_components.wavespa.coordinator import WavespaUpdateCoordinator


def _make_api(*device_ids: str) -> WavespaApi:
    """Build a real API with the given devices registered and no network.

    The tests below exercise the state merging the coordinator delegates to the
    API, so a real instance is used rather than a mock. Nothing here issues a
    request, so the session is never touched.
    """
    api = WavespaApi(session=AsyncMock(), user_token="token", api_root="http://api")
    api.devices = {device_id: MagicMock() for device_id in device_ids}
    for device in api.devices.values():
        device.is_online = True
    return api


def _cached(api: WavespaApi) -> dict[str, WavespaDeviceStatus]:
    """Return the API's cached device states."""
    return api.cached_results().devices


@pytest.mark.asyncio
async def test_coordinator_websocket_update(hass: HomeAssistant):
    """Test coordinator receives and processes WebSocket updates."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_ROOT: CONF_API_ROOT_EU},
        entry_id="test",
    )

    # The coordinator ignores updates for unknown devices, so register it
    api = _make_api("device123")

    # Create coordinator
    coordinator = WavespaUpdateCoordinator(hass, config_entry, api)

    # Simulate WebSocket update
    test_attrs = {
        "power": 1,
        "temp_now": 36,
        "temp_set": 38,
        "heat_power": 1,
    }

    coordinator.handle_websocket_update("device123", test_attrs)

    # Verify state cache updated
    assert "device123" in _cached(api)
    cached_status = _cached(api)["device123"]
    assert isinstance(cached_status, WavespaDeviceStatus)
    assert cached_status.attrs == test_attrs
    assert cached_status.timestamp > 0

    # Verify WebSocket update tracked
    assert "device123" in coordinator._ws_last_update
    assert coordinator._ws_last_update["device123"] > 0


@pytest.mark.asyncio
async def test_coordinator_websocket_disconnect(hass: HomeAssistant):
    """Test polling fallback on WebSocket disconnect."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_ROOT: CONF_API_ROOT_EU},
        entry_id="test",
    )

    api = MagicMock(spec=WavespaApi)
    coordinator = WavespaUpdateCoordinator(hass, config_entry, api)

    # Set WebSocket active mode (5min polling)
    coordinator.set_websocket_active()
    assert coordinator.update_interval == timedelta(seconds=300)

    # Simulate disconnect
    coordinator.handle_websocket_disconnect()

    # Verify polling reverted to 30s
    assert coordinator.update_interval == timedelta(seconds=30)


@pytest.mark.asyncio
async def test_coordinator_set_websocket_active(hass: HomeAssistant):
    """Test setting coordinator to WebSocket-active mode."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_ROOT: CONF_API_ROOT_EU},
        entry_id="test",
    )

    api = MagicMock(spec=WavespaApi)
    coordinator = WavespaUpdateCoordinator(hass, config_entry, api)

    # Initial state: 30s polling
    assert coordinator.update_interval == timedelta(seconds=30)

    # Activate WebSocket mode
    coordinator.set_websocket_active()

    # Verify reduced to 5min
    assert coordinator.update_interval == timedelta(seconds=300)


@pytest.mark.asyncio
async def test_polling_returns_to_slow_rate_after_reconnect(hass: HomeAssistant):
    """Test a recovered WebSocket restores the 5-minute polling interval.

    set_websocket_active() used to be called once at setup only, so the first
    dropped connection pinned polling at 30 seconds until the entry was
    reloaded - hammering the cloud API ten times more often than intended even
    though pushes had resumed.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_ROOT: CONF_API_ROOT_EU},
        entry_id="test",
    )

    api = MagicMock(spec=WavespaApi)
    coordinator = WavespaUpdateCoordinator(hass, config_entry, api)

    coordinator.set_websocket_active()
    assert coordinator.update_interval == timedelta(seconds=300)

    coordinator.handle_websocket_disconnect()
    assert coordinator.update_interval == timedelta(seconds=30)

    # The feed comes back
    coordinator.set_websocket_active()
    assert coordinator.update_interval == timedelta(seconds=300)

    # And survives a second outage cycle
    coordinator.handle_websocket_disconnect()
    coordinator.set_websocket_active()
    assert coordinator.update_interval == timedelta(seconds=300)


@pytest.mark.asyncio
async def test_repeated_interval_changes_are_idempotent(hass: HomeAssistant):
    """Test re-announcing the same state doesn't churn the interval.

    A flapping connection can call these repeatedly; each should settle on the
    same interval rather than depending on how many times it was called.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_ROOT: CONF_API_ROOT_EU},
        entry_id="test",
    )

    api = MagicMock(spec=WavespaApi)
    coordinator = WavespaUpdateCoordinator(hass, config_entry, api)

    for _ in range(3):
        coordinator.set_websocket_active()
    assert coordinator.update_interval == timedelta(seconds=300)

    for _ in range(3):
        coordinator.handle_websocket_disconnect()
    assert coordinator.update_interval == timedelta(seconds=30)


@pytest.mark.asyncio
async def test_multi_device_websocket_updates(hass: HomeAssistant):
    """Test WebSocket updates work correctly with multiple devices."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_ROOT: CONF_API_ROOT_EU},
        entry_id="test",
    )

    api = _make_api("device1", "device2")

    coordinator = WavespaUpdateCoordinator(hass, config_entry, api)

    # Update device 1
    coordinator.handle_websocket_update("device1", {"power": 1, "temp_now": 38})

    # Update device 2
    coordinator.handle_websocket_update("device2", {"power": 0, "temp_now": 25})

    # Verify both devices updated independently
    cached = _cached(api)
    assert "device1" in cached
    assert "device2" in cached
    assert cached["device1"].attrs["power"] == 1
    assert cached["device1"].attrs["temp_now"] == 38
    assert cached["device2"].attrs["power"] == 0
    assert cached["device2"].attrs["temp_now"] == 25


@pytest.mark.asyncio
async def test_websocket_update_creates_device_status(hass: HomeAssistant):
    """Test WebSocket update creates WavespaDeviceStatus correctly."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_ROOT: CONF_API_ROOT_EU},
        entry_id="test",
    )

    api = _make_api("device_abc")

    coordinator = WavespaUpdateCoordinator(hass, config_entry, api)

    # Record time before update
    before_time = int(time())

    # Simulate WebSocket update
    coordinator.handle_websocket_update("device_abc", {"power": 1})

    # Verify WavespaDeviceStatus created with current timestamp
    status = _cached(api)["device_abc"]
    assert status.timestamp >= before_time
    assert status.timestamp <= int(time())
    assert status.attrs == {"power": 1}


@pytest.mark.asyncio
async def test_coordinator_tracks_websocket_update_times(hass: HomeAssistant):
    """Test coordinator tracks last WebSocket update time per device."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_ROOT: CONF_API_ROOT_EU},
        entry_id="test",
    )

    api = _make_api("device1")

    coordinator = WavespaUpdateCoordinator(hass, config_entry, api)

    # Initially no tracked updates
    assert len(coordinator._ws_last_update) == 0

    # Update device
    coordinator.handle_websocket_update("device1", {"power": 1})

    # Verify update time tracked
    assert "device1" in coordinator._ws_last_update
    update_time = coordinator._ws_last_update["device1"]
    assert update_time > 0

    # Update again
    import time as time_module

    time_module.sleep(0.01)  # Small delay
    coordinator.handle_websocket_update("device1", {"power": 0})

    # Verify time updated
    new_update_time = coordinator._ws_last_update["device1"]
    assert new_update_time > update_time


@pytest.mark.asyncio
async def test_online_status_push_updates_the_device(hass: HomeAssistant):
    """An s2c_online_status push moves the flag immediately.

    It used to be logged and discarded, so the flag only changed when a
    bindings refresh happened to notice - up to a full polling interval after
    the spa had actually dropped.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_API_ROOT: CONF_API_ROOT_EU}, entry_id="test"
    )
    api = _make_api("device1")
    api.devices["device1"].is_online = True
    coordinator = WavespaUpdateCoordinator(hass, config_entry, api)

    coordinator.handle_websocket_online_status("device1", False)
    assert api.devices["device1"].is_online is False

    coordinator.handle_websocket_online_status("device1", True)
    assert api.devices["device1"].is_online is True


@pytest.mark.asyncio
async def test_online_status_push_for_unknown_device_is_ignored(hass: HomeAssistant):
    config_entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_API_ROOT: CONF_API_ROOT_EU}, entry_id="test"
    )
    api = _make_api("device1")
    coordinator = WavespaUpdateCoordinator(hass, config_entry, api)

    # Must not raise, and must not invent a device
    coordinator.handle_websocket_online_status("nope", False)
    assert "nope" not in api.devices


@pytest.mark.asyncio
async def test_unchanged_online_status_does_not_notify(hass: HomeAssistant):
    """A repeated push for the same state should not churn entity updates."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_API_ROOT: CONF_API_ROOT_EU}, entry_id="test"
    )
    api = _make_api("device1")
    api.devices["device1"].is_online = True
    coordinator = WavespaUpdateCoordinator(hass, config_entry, api)

    with patch.object(coordinator, "async_set_updated_data") as notify:
        coordinator.handle_websocket_online_status("device1", True)
        notify.assert_not_called()

        coordinator.handle_websocket_online_status("device1", False)
        notify.assert_called_once()


@pytest.mark.asyncio
async def test_update_for_unrecognised_device_is_ignored(hass: HomeAssistant):
    """A push for a device we do not know must not create cache state."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_API_ROOT: CONF_API_ROOT_EU}, entry_id="test"
    )
    api = _make_api("device1")
    coordinator = WavespaUpdateCoordinator(hass, config_entry, api)

    coordinator.handle_websocket_update("unknown-device", {"Heater": 1})

    assert "unknown-device" not in _cached(api)
    assert "unknown-device" not in coordinator._ws_last_update
