"""Tests for the HTTP layer of wavespa/api.py.

Every other API test stubs `_do_get`, so the transport and the error-code
mapping underneath it were never exercised. That mapping is the highest
consequence code in the module: error 9004 is what turns an expired token into
a reauthentication prompt, and getting it wrong would silently undo ed6f7fa,
which made reauth reachable in the first place.
"""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.wavespa.wavespa.api import (
    WavespaApi,
    WavespaIncorrectPasswordException,
    WavespaOfflineException,
    WavespaTokenInvalidException,
    WavespaUserDoesNotExistException,
    _raise_for_status,
)
from custom_components.wavespa.wavespa.model import WavespaDeviceType


def _response(
    *,
    ok: bool = True,
    content_type: str = "application/json",
    json_body: Any = None,
    json_raises: Exception | None = None,
) -> MagicMock:
    """Build a stand-in for an aiohttp response."""
    response = MagicMock()
    response.ok = ok
    response.content_type = content_type
    if json_raises is not None:
        response.json = AsyncMock(side_effect=json_raises)
    else:
        response.json = AsyncMock(return_value=json_body)
    response.raise_for_status = MagicMock(
        side_effect=None if ok else RuntimeError("HTTP error")
    )
    return response


def _api(response: MagicMock) -> WavespaApi:
    """Build an API whose session returns the given response."""
    session = MagicMock()
    session.get = AsyncMock(return_value=response)
    session.post = AsyncMock(return_value=response)
    return WavespaApi(session=session, user_token="tok3n", api_root="http://api")


def _session(api: WavespaApi) -> MagicMock:
    """Return the stubbed session, typed so mock assertions type-check.

    WavespaApi annotates _session as ClientSession, so mypy resolves .get
    and .post to the real signatures rather than the mock's.
    """
    return cast(MagicMock, api._session)


class TestRaiseForStatus:
    """Maps Gizwits error codes to the exceptions the rest of the code acts on."""

    async def test_ok_response_passes_through(self) -> None:
        await _raise_for_status(_response(ok=True))

    @pytest.mark.parametrize(
        ("error_code", "expected"),
        [
            (9004, WavespaTokenInvalidException),
            (9005, WavespaUserDoesNotExistException),
            (9042, WavespaOfflineException),
            (9020, WavespaIncorrectPasswordException),
        ],
    )
    async def test_error_codes_map_to_exceptions(self, error_code, expected) -> None:
        response = _response(ok=False, json_body={"error_code": error_code})

        with pytest.raises(expected):
            await _raise_for_status(response)

    async def test_token_invalid_is_an_auth_error(self) -> None:
        """9004 must be catchable as WavespaAuthException.

        The coordinator catches that base class and re-raises it as
        ConfigEntryAuthFailed; if 9004 stopped being one, an expired token
        would give permanently unavailable entities and no reauth prompt.
        """
        from custom_components.wavespa.wavespa.api import WavespaAuthException

        response = _response(ok=False, json_body={"error_code": 9004})

        with pytest.raises(WavespaAuthException):
            await _raise_for_status(response)

    async def test_unrecognised_error_code_falls_through(self) -> None:
        response = _response(ok=False, json_body={"error_code": 1234})

        with pytest.raises(RuntimeError):
            await _raise_for_status(response)

        response.raise_for_status.assert_called_once()

    async def test_body_without_an_error_code_falls_through(self) -> None:
        response = _response(ok=False, json_body={"message": "nope"})

        with pytest.raises(RuntimeError):
            await _raise_for_status(response)

    async def test_non_json_body_falls_through(self) -> None:
        response = _response(ok=False, content_type="text/html")

        with pytest.raises(RuntimeError):
            await _raise_for_status(response)

        response.json.assert_not_called()

    async def test_unparsable_json_falls_through(self) -> None:
        """A body that will not parse must not mask the HTTP error."""
        response = _response(ok=False, json_raises=ValueError("not json"))

        with pytest.raises(RuntimeError):
            await _raise_for_status(response)


class TestRequests:
    """The GET and POST helpers, including their headers."""

    async def test_get_sends_the_user_token(self) -> None:
        api = _api(_response(json_body={"hello": "world"}))

        result = await api._do_get("http://api/thing")

        assert result == {"hello": "world"}
        headers = _session(api).get.call_args.kwargs["headers"]
        assert headers["X-Gizwits-User-token"] == "tok3n"

    async def test_get_ignores_the_declared_content_type(self) -> None:
        """The API often mislabels JSON as text/html, so the check is disabled."""
        response = _response(json_body={"ok": True})
        api = _api(response)

        await api._do_get("http://api/thing")

        assert response.json.call_args.kwargs == {"content_type": None}

    async def test_get_raises_on_an_error_response(self) -> None:
        api = _api(_response(ok=False, json_body={"error_code": 9004}))

        with pytest.raises(WavespaTokenInvalidException):
            await api._do_get("http://api/thing")

    async def test_control_post_sends_attrs(self) -> None:
        response = _response(json_body={"done": True})
        api = _api(response)

        await api._do_control_post("did123", Heater=1)

        assert _session(api).post.call_args.args[0].endswith("/app/control/did123")
        assert _session(api).post.call_args.kwargs["json"] == {"attrs": {"Heater": 1}}

    async def test_post_sends_the_user_token(self) -> None:
        api = _api(_response(json_body={}))

        await api._do_post("http://api/thing", {"a": 1})

        headers = _session(api).post.call_args.kwargs["headers"]
        assert headers["X-Gizwits-User-token"] == "tok3n"


class TestGetUserToken:
    """Login, the one call made before an API instance exists."""

    async def test_returns_the_token(self) -> None:
        session = MagicMock()
        session.post = AsyncMock(
            return_value=_response(
                json_body={"uid": "uid-1", "token": "tok", "expire_at": 42}
            )
        )

        token = await WavespaApi.get_user_token(
            session, "user@example.org", "pw", "http://api"
        )

        assert (token.user_id, token.user_token, token.expiry) == ("uid-1", "tok", 42)
        assert session.post.call_args.kwargs["json"] == {
            "username": "user@example.org",
            "password": "pw",
            "lang": "en",
        }

    async def test_bad_password_raises(self) -> None:
        session = MagicMock()
        session.post = AsyncMock(
            return_value=_response(ok=False, json_body={"error_code": 9020})
        )

        with pytest.raises(WavespaIncorrectPasswordException):
            await WavespaApi.get_user_token(session, "user", "wrong", "http://api")


class TestRefreshBindings:
    """Turns the bindings response into the device map."""

    _BINDING = {
        "protoc": 2,
        "did": "did123",
        "product_name": "Wave_SPA_EU",
        "dev_alias": "Test Spa",
        "mcu_soft_version": "1.0",
        "mcu_hard_version": "1.1",
        "wifi_soft_version": "2.0",
        "wifi_hard_version": "2.1",
        "is_online": True,
    }

    async def test_builds_devices(self) -> None:
        api = _api(_response(json_body={"devices": [dict(self._BINDING)]}))

        await api.refresh_bindings()

        device = api.devices["did123"]
        assert device.alias == "Test Spa"
        assert device.device_type is WavespaDeviceType.WAVESPA_EU
        assert device.is_online is True

    async def test_websocket_endpoint_defaults_when_absent(self) -> None:
        api = _api(_response(json_body={"devices": [dict(self._BINDING)]}))

        await api.refresh_bindings()

        device = api.devices["did123"]
        assert device.ws_host == "m2m.gizwits.com"
        assert device.ws_port == 8880

    async def test_websocket_endpoint_is_taken_from_the_response(self) -> None:
        """Regional endpoints come from the API rather than being hardcoded."""
        binding = dict(self._BINDING) | {"host": "eu.example.com", "wss_port": 1234}
        api = _api(_response(json_body={"devices": [binding]}))

        await api.refresh_bindings()

        assert api.devices["did123"].ws_host == "eu.example.com"
        assert api.devices["did123"].ws_port == 1234

    async def test_refresh_replaces_the_previous_map(self) -> None:
        """A device that disappears from the account stops being tracked."""
        api = _api(_response(json_body={"devices": [dict(self._BINDING)]}))
        await api.refresh_bindings()

        _session(api).get = AsyncMock(return_value=_response(json_body={"devices": []}))
        await api.refresh_bindings()

        assert api.devices == {}


class TestSanitizeBindingsResponse:
    """Redaction applied before device listings reach the log.

    People paste logs into public issues without reading them, which is the
    entire reason this exists.
    """

    _RAW = {
        "devices": [
            {
                "did": "device-identifier",
                "mac": "AABBCCDDEEFF",
                "passcode": "secret-passcode",
                "product_key": "product-key-value",
                "dev_alias": "Test Spa",
            }
        ]
    }

    def test_identifiers_are_masked(self) -> None:
        result = WavespaApi._sanitize_bindings_response(self._RAW)

        device = result["devices"][0]
        for field in ("did", "mac", "passcode", "product_key"):
            assert set(device[field]) == {"*"}, f"{field} was not masked"

    def test_no_secret_survives(self) -> None:
        import json

        blob = json.dumps(WavespaApi._sanitize_bindings_response(self._RAW))

        for secret in (
            "device-identifier",
            "AABBCCDDEEFF",
            "secret-passcode",
            "product-key-value",
        ):
            assert secret not in blob

    def test_harmless_fields_are_kept(self) -> None:
        """Masking everything would make the log useless for diagnosis."""
        result = WavespaApi._sanitize_bindings_response(self._RAW)

        assert result["devices"][0]["dev_alias"] == "Test Spa"

    def test_the_original_is_not_mutated(self) -> None:
        """The caller goes on to build devices from this same response."""
        original = {"devices": [dict(self._RAW["devices"][0])]}

        WavespaApi._sanitize_bindings_response(original)

        assert original["devices"][0]["did"] == "device-identifier"

    def test_missing_fields_are_tolerated(self) -> None:
        """The response shape is not guaranteed; logging must not raise."""
        result = WavespaApi._sanitize_bindings_response({"devices": [{"did": "x"}]})

        assert result["devices"][0]["did"] == "*"

    def test_no_devices_key_is_tolerated(self) -> None:
        assert WavespaApi._sanitize_bindings_response({}) == {}


class TestFetchDataEdgeCases:
    """Paths through fetch_data that the control-focused tests do not reach."""

    @staticmethod
    def _api_with_response(payload: dict[str, Any], product: str = "Wave_SPA_EU"):
        api = _api(_response(json_body=payload))
        from custom_components.wavespa.wavespa.model import WavespaDevice

        api.devices = {
            "did123": WavespaDevice(
                protocol_version=2,
                device_id="did123",
                product_name=product,
                alias="Test Spa",
                mcu_soft_version="1.0",
                mcu_hard_version="1.1",
                wifi_soft_version="2.0",
                wifi_hard_version="2.1",
                is_online=True,
            )
        }
        return api

    async def test_zero_timestamp_is_skipped(self) -> None:
        """updated_at of 0 means the device has never reported.

        Observed on a spa that had been offline for months; the attrs come
        back empty, so caching them would wipe the last known state.
        """
        api = self._api_with_response({"updated_at": 0, "attr": {}})

        results = await api.fetch_data()

        assert "did123" not in results.devices

    async def test_poll_merges_onto_the_cached_attrs(self) -> None:
        """Time_filter has been seen missing from otherwise full snapshots."""
        api = self._api_with_response(
            {"updated_at": 200, "attr": {"Heater": 1, "Filter": 1}}
        )
        from custom_components.wavespa.wavespa.model import WavespaDeviceStatus

        api._state_cache["did123"] = WavespaDeviceStatus(
            timestamp=100, attrs={"Heater": 0, "Time_filter": 5000}
        )

        results = await api.fetch_data()

        attrs = results.devices["did123"].attrs
        assert attrs["Heater"] == 1  # refreshed
        assert attrs["Filter"] == 1  # new
        assert attrs["Time_filter"] == 5000  # preserved from the cache

    async def test_unknown_device_type_is_reported(self, caplog) -> None:
        """An unrecognised model logs its attrs so support can be added."""
        api = self._api_with_response(
            {"updated_at": 200, "attr": {"Heater": 1}}, product="Wave_SPA_XX"
        )

        await api.fetch_data()

        assert "unknown device type" in caplog.text.lower()
        assert "Wave_SPA_XX" in caplog.text
