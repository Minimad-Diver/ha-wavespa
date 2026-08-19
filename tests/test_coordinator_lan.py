"""Tests for how the coordinator handles LAN status updates.

Companion to test_coordinator_websocket.py: the LAN is the second push
transport, and most of what is pinned here is the interaction between the two
rather than either alone.
"""

from __future__ import annotations

from datetime import timedelta
from typing import cast
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wavespa.const import CONF_API_ROOT, CONF_API_ROOT_EU, DOMAIN
from custom_components.wavespa.coordinator import WavespaUpdateCoordinator
from custom_components.wavespa.wavespa.api import WavespaApi, WavespaApiResults
from custom_components.wavespa.wavespa.model import WavespaDevice, WavespaDeviceStatus


def _device(device_id: str = "did") -> WavespaDevice:
    return WavespaDevice(
        protocol_version=1,
        device_id=device_id,
        product_name="Wave_SPA_EU",
        alias="Spa",
        mcu_soft_version="1",
        mcu_hard_version="1",
        wifi_soft_version="1",
        wifi_hard_version="1",
        is_online=True,
        product_key="pk123",
    )


def _merge_mock(coordinator: WavespaUpdateCoordinator) -> MagicMock:
    """The stubbed merge_device_attrs, typed so mock assertions type-check.

    The coordinator annotates api as WavespaApi, so mypy resolves the method
    to the real signature rather than the mock's.
    """
    return cast(MagicMock, coordinator.api.merge_device_attrs)


class TestCoordinatorUpdates:
    """What the session hands back, and what the entities see."""

    def _coordinator(self, hass: HomeAssistant) -> WavespaUpdateCoordinator:
        entry = MockConfigEntry(
            domain=DOMAIN, data={CONF_API_ROOT: CONF_API_ROOT_EU}, entry_id="test"
        )
        api = MagicMock(spec=WavespaApi)
        api.devices = {"did": _device()}
        coordinator = WavespaUpdateCoordinator(hass, entry, api)
        coordinator.set_lan_device("did")
        return coordinator

    async def test_an_update_reaches_the_cache(self, hass: HomeAssistant) -> None:
        coordinator = self._coordinator(hass)

        coordinator.handle_lan_update({"Heater": 1, "Bubble": 0})

        _merge_mock(coordinator).assert_called_once_with(
            "did", {"Heater": 1, "Bubble": 0}
        )

    async def test_an_identical_frame_is_dropped(self, hass: HomeAssistant) -> None:
        """Measured on real hardware: one bubble toggle produced three frames
        in 2.5 seconds, one of them byte-identical to the one before it.

        Without this every toggle wakes every entity two or three times over.
        """
        coordinator = self._coordinator(hass)
        coordinator.data = WavespaApiResults(
            {"did": WavespaDeviceStatus(0, {"Heater": 1, "Bubble": 0})}
        )

        coordinator.handle_lan_update({"Heater": 1, "Bubble": 0})

        _merge_mock(coordinator).assert_not_called()

    async def test_a_changed_frame_is_applied(self, hass: HomeAssistant) -> None:
        """The other half of the dedupe: a real change must still get through."""
        coordinator = self._coordinator(hass)
        coordinator.data = WavespaApiResults(
            {"did": WavespaDeviceStatus(0, {"Heater": 1, "Bubble": 0})}
        )

        coordinator.handle_lan_update({"Heater": 1, "Bubble": 1})

        _merge_mock(coordinator).assert_called_once()

    async def test_an_update_with_no_device_is_discarded(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        coordinator = self._coordinator(hass)
        coordinator.set_lan_device("unknown")

        coordinator.handle_lan_update({"Heater": 1})

        _merge_mock(coordinator).assert_not_called()
        assert "no device to apply it to" in caplog.text

    async def test_last_lan_update_is_recorded(self, hass: HomeAssistant) -> None:
        coordinator = self._coordinator(hass)
        assert coordinator.last_lan_update("did") is None

        coordinator.handle_lan_update({"Heater": 1})

        assert coordinator.last_lan_update("did") is not None
        assert coordinator.last_lan_update("other") is None


class TestLivePush:
    """Whether a transport is currently connected, for entity availability."""

    def _coordinator(self, hass: HomeAssistant) -> WavespaUpdateCoordinator:
        entry = MockConfigEntry(
            domain=DOMAIN, data={CONF_API_ROOT: CONF_API_ROOT_EU}, entry_id="test"
        )
        api = MagicMock(spec=WavespaApi)
        api.devices = {"did": _device()}
        coordinator = WavespaUpdateCoordinator(hass, entry, api)
        coordinator.websocket = None
        coordinator.lan = None
        return coordinator

    async def test_nothing_connected(self, hass: HomeAssistant) -> None:
        assert self._coordinator(hass).has_live_push("did") is False

    async def test_a_connected_websocket_counts(self, hass: HomeAssistant) -> None:
        coordinator = self._coordinator(hass)
        coordinator.websocket = MagicMock(is_connected=True)

        assert coordinator.has_live_push("did") is True

    async def test_a_disconnected_websocket_does_not(self, hass: HomeAssistant) -> None:
        coordinator = self._coordinator(hass)
        coordinator.websocket = MagicMock(is_connected=False)

        assert coordinator.has_live_push("did") is False

    async def test_a_connected_lan_session_counts(self, hass: HomeAssistant) -> None:
        coordinator = self._coordinator(hass)
        coordinator.lan = MagicMock(is_connected=True)
        coordinator.set_lan_device("did")

        assert coordinator.has_live_push("did") is True

    async def test_the_lan_counts_only_for_its_own_spa(
        self, hass: HomeAssistant
    ) -> None:
        """One session speaks for one spa.

        Otherwise a second spa would be reported reachable on the strength of
        the first one's connection.
        """
        coordinator = self._coordinator(hass)
        coordinator.lan = MagicMock(is_connected=True)
        coordinator.set_lan_device("did")

        assert coordinator.has_live_push("a-different-spa") is False


class TestPollingInterval:
    """Polling is the safety net, so it tracks both push transports."""

    def _coordinator(self, hass: HomeAssistant) -> WavespaUpdateCoordinator:
        entry = MockConfigEntry(
            domain=DOMAIN, data={CONF_API_ROOT: CONF_API_ROOT_EU}, entry_id="test"
        )
        return WavespaUpdateCoordinator(hass, entry, MagicMock(spec=WavespaApi))

    async def test_lan_alone_slows_polling(self, hass: HomeAssistant) -> None:
        coordinator = self._coordinator(hass)

        coordinator.set_lan_active()

        assert coordinator.update_interval == timedelta(seconds=300)

    async def test_losing_the_lan_speeds_polling_back_up(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = self._coordinator(hass)
        coordinator.set_lan_active()

        coordinator.handle_lan_disconnect()

        assert coordinator.update_interval == timedelta(seconds=30)

    async def test_losing_one_transport_keeps_the_others_saving(
        self, hass: HomeAssistant
    ) -> None:
        """The reason the two are tracked separately.

        With a single interval flag, the LAN dropping would have reverted a
        perfectly healthy WebSocket feed to 30-second polling.
        """
        coordinator = self._coordinator(hass)
        coordinator.set_websocket_active()
        coordinator.set_lan_active()

        coordinator.handle_lan_disconnect()

        assert coordinator.update_interval == timedelta(seconds=300)

        coordinator.handle_websocket_disconnect()
        assert coordinator.update_interval == timedelta(seconds=30)

    async def test_repeated_calls_settle(self, hass: HomeAssistant) -> None:
        """A flapping connection must not depend on how often it fired."""
        coordinator = self._coordinator(hass)

        for _ in range(3):
            coordinator.set_lan_active()
        assert coordinator.update_interval == timedelta(seconds=300)

        for _ in range(3):
            coordinator.handle_lan_disconnect()
        assert coordinator.update_interval == timedelta(seconds=30)
