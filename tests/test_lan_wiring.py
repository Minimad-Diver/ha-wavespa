"""Tests for wiring the LAN transport into the integration.

The LAN is opt-in and best-effort: every way it can fail must leave the cloud
transport running, because local control is an enhancement and losing it must
never cost the user their spa entities. Most of what follows pins that.

The session and codec themselves are covered by test_lan_session.py and
test_lan_codec.py; this is about setup, teardown and the coordinator.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from contextlib import ExitStack
from datetime import datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wavespa.const import (
    CONF_API_ROOT,
    CONF_API_ROOT_EU,
    CONF_LAN_HOST,
    CONF_PASSWORD,
    CONF_UID,
    CONF_USER_TOKEN,
    CONF_USER_TOKEN_EXPIRY,
    CONF_USERNAME,
    DOMAIN,
)
from custom_components.wavespa.coordinator import WavespaUpdateCoordinator
from custom_components.wavespa.wavespa.api import WavespaApi, WavespaApiResults
from custom_components.wavespa.wavespa.model import WavespaDevice

_DEFINITION = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "datapoint_wave_spa.json").read_text(
        encoding="utf8"
    )
)

_HOST = "192.0.2.10"


def _device(device_id: str = "did", product_key: str = "pk123") -> WavespaDevice:
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
        product_key=product_key,
    )


class _FakeWebSocket:
    """Stand-in for GizwitsWebSocket, so unload can await its disconnect."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def async_run(self) -> None:
        while True:
            await asyncio.sleep(0.01)

    async def disconnect(self) -> None:
        return None


class _FakeSession:
    """Stand-in for GizwitsLanSession that records its own lifecycle."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.started = False
        self.cancelled = False
        self.disconnected = False

    async def async_run(self) -> None:
        self.started = True
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def disconnect(self) -> None:
        self.disconnected = True


def _merge_mock(coordinator: WavespaUpdateCoordinator) -> MagicMock:
    """The stubbed merge_device_attrs, typed so mock assertions type-check.

    The coordinator annotates api as WavespaApi, so mypy resolves the method
    to the real signature rather than the mock's.
    """
    return cast(MagicMock, coordinator.api.merge_device_attrs)


def _entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    future = (datetime.now() + timedelta(days=31)).timestamp()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_USER_TOKEN: "t0k3n",
            CONF_USER_TOKEN_EXPIRY: int(future),
            CONF_UID: "uid",
        },
        options=options,
        version=2,
        entry_id="test",
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    devices: dict[str, WavespaDevice],
    *,
    session: Any = None,
    definition: Any = None,
    real_session_class: bool = False,
    fetch: Any = None,
) -> Any:
    """Set the entry up with the cloud stubbed out, returning the session mock.

    real_session_class leaves GizwitsLanSession unpatched, which is how the
    schema validation in its constructor gets exercised - a mock would accept
    any schema at all and the refusal being tested would never happen.
    """

    async def populate(self: WavespaApi) -> None:
        self.devices = dict(devices)

    async def fetch_cached(self: WavespaApi) -> WavespaApiResults:
        return self.cached_results()

    async def get_definition(self: WavespaApi, product_key: str) -> Any:
        if fetch is not None:
            return fetch(product_key)
        if isinstance(definition, Exception):
            raise definition
        return _DEFINITION if definition is None else definition

    lan_cls = MagicMock(return_value=session or _FakeSession())
    patches: list[Any] = [
        patch.object(WavespaApi, "refresh_bindings", populate),
        patch.object(WavespaApi, "fetch_data", fetch_cached),
        patch.object(WavespaApi, "get_datapoint_definition", get_definition),
        patch("custom_components.wavespa.GizwitsWebSocket", _FakeWebSocket),
    ]
    if not real_session_class:
        patches.append(patch("custom_components.wavespa.GizwitsLanSession", lan_cls))

    with ExitStack() as stack:
        for each in patches:
            stack.enter_context(each)
        await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.sleep(0.05)

    return lan_cls


class TestSetupIsOptIn:
    """Local control only happens when it has been asked for and can work."""

    async def test_no_host_means_no_session(self, hass: HomeAssistant) -> None:
        """The default, and it must behave exactly as it always has."""
        entry = _entry(hass)

        lan_cls = await _setup(hass, entry, {"did": _device()})

        lan_cls.assert_not_called()
        assert entry.runtime_data.lan is None

    async def test_blank_host_is_treated_as_off(self, hass: HomeAssistant) -> None:
        """Clearing the box is the supported way to turn local control off."""
        entry = _entry(hass, **{CONF_LAN_HOST: "   "})

        lan_cls = await _setup(hass, entry, {"did": _device()})

        lan_cls.assert_not_called()
        assert entry.runtime_data.lan is None

    async def test_a_configured_host_starts_a_session(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(hass, **{CONF_LAN_HOST: _HOST})
        session = _FakeSession()

        lan_cls = await _setup(hass, entry, {"did": _device()}, session=session)

        assert lan_cls.call_args.args[0] == _HOST
        assert session.started
        assert entry.runtime_data.lan is session

    async def test_the_session_is_told_which_device_it_speaks_for(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(hass, **{CONF_LAN_HOST: _HOST})

        await _setup(hass, entry, {"did": _device()})

        assert entry.runtime_data._lan_device_id == "did"


class TestSetupRefusals:
    """Every refusal must leave the cloud transport running."""

    async def test_multiple_devices_are_refused(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One address cannot be attributed to one of several spas.

        Guessing would put one spa's readings on another, which is far worse
        than not offering local control at all.
        """
        entry = _entry(hass, **{CONF_LAN_HOST: _HOST})

        lan_cls = await _setup(hass, entry, {"a": _device("a"), "b": _device("b")})

        lan_cls.assert_not_called()
        assert entry.runtime_data.lan is None
        assert "account has 2 devices" in caplog.text

    async def test_a_missing_product_key_is_refused(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Without it the datapoint layout cannot be fetched."""
        entry = _entry(hass, **{CONF_LAN_HOST: _HOST})

        lan_cls = await _setup(hass, entry, {"did": _device(product_key="")})

        lan_cls.assert_not_called()
        assert entry.runtime_data.lan is None
        assert "product_key" in caplog.text

    async def test_a_failed_definition_fetch_is_survivable(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The cloud does not need the definition, so setup must still finish."""
        entry = _entry(hass, **{CONF_LAN_HOST: _HOST})

        lan_cls = await _setup(
            hass, entry, {"did": _device()}, definition=RuntimeError("no route")
        )

        lan_cls.assert_not_called()
        assert entry.runtime_data.lan is None
        # The cloud transport is still up, which is the whole point
        assert entry.runtime_data.api.devices

    async def test_a_failed_definition_fetch_is_retried(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A transient failure must not disable local access until a reload.

        Home Assistant starting before the network is ready used to switch
        local access off for the whole life of the config entry, recoverable
        only by a reload the user had no reason to think of - the integration
        carries on over the cloud, so nothing looks wrong.
        """
        entry = _entry(hass, **{CONF_LAN_HOST: _HOST})

        await _setup(
            hass, entry, {"did": _device()}, definition=RuntimeError("no route")
        )

        assert "Retrying" in caplog.text

    async def test_a_definition_fetch_that_recovers_starts_the_session(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of retrying: local access comes up on its own."""
        monkeypatch.setattr(
            "custom_components.wavespa._DEFINITION_RETRY_DELAYS", (0, 0, 0)
        )
        entry = _entry(hass, **{CONF_LAN_HOST: _HOST})
        session = _FakeSession()
        attempts: list[int] = []

        def flaky(_product_key: str) -> Any:
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("network not ready")
            return _DEFINITION

        await _setup(hass, entry, {"did": _device()}, session=session, fetch=flaky)

        assert len(attempts) == 3
        assert entry.runtime_data.lan is session
        assert session.started

    async def test_an_incompatible_product_is_refused(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A definition that cannot drive our entities is permanent, not a blip.

        Uses the real session class: the check lives in its constructor, and a
        mocked one would accept any schema and never refuse.
        """
        entry = _entry(hass, **{CONF_LAN_HOST: _HOST})
        foreign = {
            "name": "Some_Other_Spa",
            "entities": [
                {
                    "attrs": [
                        {
                            "name": "WaterTemp",
                            "data_type": "uint8",
                            "position": {"byte_offset": 0},
                        }
                    ]
                }
            ],
        }

        await _setup(
            hass,
            entry,
            {"did": _device()},
            definition=foreign,
            real_session_class=True,
        )

        assert entry.runtime_data.lan is None
        assert "not available for this spa" in caplog.text


class TestTeardown:
    """The spa has few connection slots, so shutdown must be clean."""

    async def test_the_session_is_closed_on_unload(self, hass: HomeAssistant) -> None:
        """A reload that abandoned the socket would burn a slot each time."""
        entry = _entry(hass, **{CONF_LAN_HOST: _HOST})
        session = _FakeSession()

        await _setup(hass, entry, {"did": _device()}, session=session)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await asyncio.sleep(0.05)

        assert session.disconnected
        assert session.cancelled
