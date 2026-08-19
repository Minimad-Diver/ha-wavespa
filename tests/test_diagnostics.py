"""Tests for diagnostics.py.

The point of the download is the raw attr dictionary, so these check that the
attrs survive verbatim while credentials and identifiers do not.
"""

import json
from typing import Any
from unittest.mock import MagicMock

from custom_components.wavespa.const import (
    CONF_PASSWORD,
    CONF_UID,
    CONF_USER_TOKEN,
    CONF_USERNAME,
)
from custom_components.wavespa.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.wavespa.lan.session import ModuleInfo
from custom_components.wavespa.wavespa.api import WavespaApiResults
from custom_components.wavespa.wavespa.model import WavespaDevice, WavespaDeviceStatus

_ATTRS = {
    "Heater": 1,
    "Filter": 1,
    "Bubble": 0,
    "Current_temperature": 30,
    "Temperature_setup": 40,
    "Time_filter": 5000,
}


def _make_entry() -> MagicMock:
    device = WavespaDevice(
        protocol_version=2,
        device_id="did123456",
        product_name="Wave_SPA_EU",
        alias="Test Spa",
        mcu_soft_version="1.0",
        mcu_hard_version="1.1",
        wifi_soft_version="2.0",
        wifi_hard_version="2.1",
        is_online=True,
        product_key="747be354e00449e799883a966d0c9cbd",
    )
    coordinator = MagicMock()
    coordinator.api.devices = {"did123456": device}
    coordinator.data = WavespaApiResults(
        devices={"did123456": WavespaDeviceStatus(timestamp=1000, attrs=dict(_ATTRS))}
    )
    coordinator.last_update_success = True
    coordinator.websocket.is_connected = True
    coordinator.last_websocket_update = MagicMock(return_value=None)
    coordinator.lan.is_connected = True
    coordinator.lan.module_info = ModuleInfo(
        module="ESP826", hardware_id="0402003A", product_key="pk-value"
    )
    coordinator.last_lan_update = MagicMock(return_value=None)

    entry = MagicMock()
    entry.version = 2
    entry.data = {
        CONF_USERNAME: "person@example.org",
        CONF_PASSWORD: "hunter2",
        CONF_USER_TOKEN: "secret-token",
        CONF_UID: "uid-123",
        "apiroot": "https://euapi.gizwits.com",
    }
    entry.options = {}
    entry.runtime_data = coordinator
    return entry


async def _diag() -> dict[str, Any]:
    return await async_get_config_entry_diagnostics(MagicMock(), _make_entry())


class TestRedaction:
    """Credentials and identifiers must not survive into a public issue."""

    async def test_credentials_are_redacted(self) -> None:
        diag = await _diag()
        data = diag["entry"]["data"]
        for key in (CONF_USERNAME, CONF_PASSWORD, CONF_USER_TOKEN, CONF_UID):
            assert data[key] == "**REDACTED**"

    async def test_device_id_is_redacted(self) -> None:
        diag = await _diag()
        assert diag["devices"][0]["device_id"] == "**REDACTED**"

    async def test_no_secret_appears_anywhere(self) -> None:
        """Belt and braces: scan the serialised output for the raw values."""
        import json

        blob = json.dumps(await _diag())
        for secret in ("hunter2", "secret-token", "uid-123", "did123456"):
            assert secret not in blob

    async def test_api_root_is_kept(self) -> None:
        """Not a secret, and it tells you which region a report came from."""
        diag = await _diag()
        assert diag["entry"]["data"]["apiroot"] == "https://euapi.gizwits.com"


class TestContent:
    """What the download is actually for."""

    async def test_raw_attrs_are_reported_verbatim(self) -> None:
        diag = await _diag()
        assert diag["devices"][0]["status"]["attrs"] == _ATTRS

    async def test_derived_values_are_included(self) -> None:
        """Saves cross-checking the derivation by hand when triaging."""
        diag = await _diag()
        status = diag["devices"][0]["status"]
        assert status["is_heating"] is True
        assert status["percent_filter"] == 50

    async def test_versions_are_reported(self) -> None:
        diag = await _diag()
        device = diag["devices"][0]
        assert device["mcu_soft_version"] == "1.0"
        assert device["wifi_hard_version"] == "2.1"
        assert device["product_name"] == "Wave_SPA_EU"

    async def test_coordinator_state_is_reported(self) -> None:
        diag = await _diag()
        assert diag["coordinator"]["last_update_success"] is True
        assert diag["coordinator"]["websocket_connected"] is True

    async def test_handles_a_device_with_no_status(self) -> None:
        entry = _make_entry()
        entry.runtime_data.data = WavespaApiResults(devices={})

        diag = await async_get_config_entry_diagnostics(MagicMock(), entry)
        assert diag["devices"][0]["status"] is None

    async def test_handles_no_websocket(self) -> None:
        entry = _make_entry()
        entry.runtime_data.websocket = None

        diag = await async_get_config_entry_diagnostics(MagicMock(), entry)
        assert diag["coordinator"]["websocket_connected"] is None

    async def test_product_key_is_reported_in_full(self) -> None:
        """It identifies the model, not the spa, and it is what decides how a
        status payload is decoded - the most useful field in a report that
        local control shows wrong values or none."""
        diag = await _diag()

        assert diag["devices"][0]["product_key"] == ("747be354e00449e799883a966d0c9cbd")

    async def test_lan_state_is_reported(self) -> None:
        diag = await _diag()
        assert diag["coordinator"]["lan_connected"] is True

    async def test_handles_no_lan_session(self) -> None:
        """None distinguishes local control being off from it failing to
        connect, which are different bugs."""
        entry = _make_entry()
        entry.runtime_data.lan = None

        diag = await async_get_config_entry_diagnostics(MagicMock(), entry)
        assert diag["coordinator"]["lan_connected"] is None

    async def test_module_info_is_reported(self) -> None:
        """The only route to the hardware id when discovery cannot run, which
        is every containerised Home Assistant."""
        diag = await _diag()

        assert diag["coordinator"]["lan_module"] == {
            "module": "ESP826",
            "hardware_id": "0402003A",
            "product_key": "pk-value",
        }

    async def test_module_info_absent_when_not_read(self) -> None:
        """It is best-effort, so the dump must cope with never having it."""
        entry = _make_entry()
        entry.runtime_data.lan.module_info = None

        diag = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert diag["coordinator"]["lan_module"] is None

    async def test_module_info_absent_without_a_session(self) -> None:
        entry = _make_entry()
        entry.runtime_data.lan = None

        diag = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert diag["coordinator"]["lan_module"] is None

    async def test_the_lan_host_is_redacted(self) -> None:
        """Not a credential, but it describes the user's own network and the
        options blob is dumped whole."""
        entry = _make_entry()
        entry.options = {"lan_host": "192.0.2.10"}

        diag = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert "192.0.2.10" not in json.dumps(diag)

    async def test_last_lan_update_is_reported(self) -> None:
        entry = _make_entry()
        entry.runtime_data.last_lan_update.return_value = 1_700_000_000.0

        diag = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert diag["devices"][0]["last_lan_update"] == "2023-11-14T22:13:20+00:00"


class TestLastWebsocketUpdate:
    """Answers "is the real-time feed actually delivering?".

    The socket being connected does not, which is why websocket_connected on
    its own was not enough for triage.
    """

    async def test_reported_as_a_readable_timestamp(self) -> None:
        entry = _make_entry()
        entry.runtime_data.last_websocket_update.return_value = 1_700_000_000.0

        diag = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert diag["devices"][0]["last_websocket_update"] == (
            "2023-11-14T22:13:20+00:00"
        )

    async def test_none_when_nothing_has_been_pushed(self) -> None:
        """Distinguishes "never pushed" from "has gone quiet"."""
        entry = _make_entry()
        entry.runtime_data.last_websocket_update.return_value = None

        diag = await async_get_config_entry_diagnostics(MagicMock(), entry)

        assert diag["devices"][0]["last_websocket_update"] is None


class TestCoordinatorAccessor:
    """The accessor that gives _ws_last_update a consumer."""

    async def test_returns_none_before_any_push(self, hass) -> None:
        from custom_components.wavespa.coordinator import WavespaUpdateCoordinator

        coordinator = WavespaUpdateCoordinator(hass, MagicMock(), MagicMock())

        assert coordinator.last_websocket_update("did") is None

    async def test_returns_the_recorded_time(self, hass) -> None:
        from custom_components.wavespa.coordinator import WavespaUpdateCoordinator

        coordinator = WavespaUpdateCoordinator(hass, MagicMock(), MagicMock())
        coordinator._ws_last_update["did"] = 1_700_000_000.0

        assert coordinator.last_websocket_update("did") == 1_700_000_000.0
