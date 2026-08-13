"""Tests for the optimistic state-cache writes in wavespa/api.py.

Each ``spa_set_*`` method POSTs a single field to the API and then updates the
local state cache to match, because a GET immediately after a POST still
returns the old values. Some methods also update *other* cached attributes to
reflect side effects the spa applies on its own.

These tests pin down which side effects each setter is allowed to have:
- the heater cannot run without the filter pump, so they move together
- the bubbles are independent of both
"""

import asyncio
import time
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.wavespa.wavespa.api import WavespaApi, WavespaException
from custom_components.wavespa.wavespa.model import WavespaDeviceStatus


_DEVICE_ID = "test_device"


def _make_api(attrs: dict[str, Any] | None = None) -> WavespaApi:
    """Build an API with a primed state cache and a stubbed control POST."""
    default_attrs: dict[str, Any] = {
        "Heater": 0,
        "Filter": 0,
        "Bubble": 0,
        "locked": 0,
        "Current_temperature": 30,
        "Temperature_setup": 40,
        "Time_filter": 5000,
    }
    if attrs:
        default_attrs.update(attrs)

    api = WavespaApi(session=AsyncMock(), user_token="token", api_root="http://api")
    api._state_cache[_DEVICE_ID] = WavespaDeviceStatus(
        timestamp=1000, attrs=default_attrs
    )
    api._do_control_post = AsyncMock()  # type: ignore[method-assign]
    return api


def _attrs(api: WavespaApi) -> dict[str, Any]:
    return api._state_cache[_DEVICE_ID].attrs


def _post(api: WavespaApi) -> AsyncMock:
    """Return the stubbed control POST, typed so mock assertions type-check."""
    return cast(AsyncMock, api._do_control_post)


class TestSpaSetFilter:
    """spa_set_filter drags the heater with it, but never the bubbles."""

    async def test_on_posts_and_caches(self) -> None:
        api = _make_api({"Filter": 0})
        await api.spa_set_filter(_DEVICE_ID, True)
        _post(api).assert_awaited_once_with(_DEVICE_ID, Filter=1)
        assert _attrs(api)["Filter"] == 1

    async def test_off_posts_and_caches(self) -> None:
        api = _make_api({"Filter": 1})
        await api.spa_set_filter(_DEVICE_ID, False)
        _post(api).assert_awaited_once_with(_DEVICE_ID, Filter=0)
        assert _attrs(api)["Filter"] == 0

    async def test_on_leaves_heater_and_bubbles_alone(self) -> None:
        """Running the pump does not imply the heater or bubbles are on."""
        api = _make_api({"Filter": 0, "Heater": 0, "Bubble": 0})
        await api.spa_set_filter(_DEVICE_ID, True)
        assert _attrs(api)["Heater"] == 0
        assert _attrs(api)["Bubble"] == 0

    async def test_off_turns_heater_off(self) -> None:
        """The heater cannot run without the filter pump."""
        api = _make_api({"Filter": 1, "Heater": 1})
        await api.spa_set_filter(_DEVICE_ID, False)
        assert _attrs(api)["Heater"] == 0

    async def test_off_leaves_bubbles_on(self) -> None:
        """Bubbles are independent of the filter pump."""
        api = _make_api({"Filter": 1, "Bubble": 1})
        await api.spa_set_filter(_DEVICE_ID, False)
        assert _attrs(api)["Bubble"] == 1

    async def test_unknown_device_raises(self) -> None:
        api = _make_api()
        with pytest.raises(WavespaException):
            await api.spa_set_filter("nope", True)


class TestSpaSetBubbles:
    """spa_set_bubbles only ever touches Bubble."""

    async def test_on_posts_and_caches(self) -> None:
        api = _make_api({"Bubble": 0})
        await api.spa_set_bubbles(_DEVICE_ID, True)
        _post(api).assert_awaited_once_with(_DEVICE_ID, Bubble=1)
        assert _attrs(api)["Bubble"] == 1

    async def test_off_posts_and_caches(self) -> None:
        api = _make_api({"Bubble": 1})
        await api.spa_set_bubbles(_DEVICE_ID, False)
        _post(api).assert_awaited_once_with(_DEVICE_ID, Bubble=0)
        assert _attrs(api)["Bubble"] == 0

    async def test_on_leaves_heater_off(self) -> None:
        """Bubbles on does not imply the heater is on."""
        api = _make_api({"Bubble": 0, "Heater": 0, "Filter": 0})
        await api.spa_set_bubbles(_DEVICE_ID, True)
        assert _attrs(api)["Heater"] == 0
        assert _attrs(api)["Filter"] == 0

    async def test_on_leaves_running_heater_on(self) -> None:
        api = _make_api({"Bubble": 0, "Heater": 1, "Filter": 1})
        await api.spa_set_bubbles(_DEVICE_ID, True)
        assert _attrs(api)["Heater"] == 1
        assert _attrs(api)["Filter"] == 1

    async def test_unknown_device_raises(self) -> None:
        api = _make_api()
        with pytest.raises(WavespaException):
            await api.spa_set_bubbles("nope", True)


class TestSpaSetHeat:
    """spa_set_heat is the inverse of spa_set_filter's heater side effect."""

    async def test_on_turns_filter_on(self) -> None:
        api = _make_api({"Heater": 0, "Filter": 0})
        await api.spa_set_heat(_DEVICE_ID, True)
        _post(api).assert_awaited_once_with(_DEVICE_ID, Heater=1)
        assert _attrs(api)["Heater"] == 1
        assert _attrs(api)["Filter"] == 1

    async def test_on_leaves_bubbles_alone(self) -> None:
        api = _make_api({"Heater": 0, "Bubble": 0})
        await api.spa_set_heat(_DEVICE_ID, True)
        assert _attrs(api)["Bubble"] == 0

    async def test_off_leaves_filter_and_bubbles_running(self) -> None:
        """The pump and bubbles can run without the heater."""
        api = _make_api({"Heater": 1, "Filter": 1, "Bubble": 1})
        await api.spa_set_heat(_DEVICE_ID, False)
        assert _attrs(api)["Heater"] == 0
        assert _attrs(api)["Filter"] == 1
        assert _attrs(api)["Bubble"] == 1


class TestControlWritesSurviveConcurrentPush:
    """A WebSocket delta landing mid-POST must not swallow the local write.

    merge_device_attrs() replaces the cached status object rather than mutating
    it, so a setter that captured the entry *before* awaiting its POST would
    write to an orphaned object and lose the change. These tests simulate a
    push arriving during the POST by mutating the cache from inside the stubbed
    request.
    """

    @staticmethod
    def _api_with_push_during_post(push: dict[str, Any]) -> WavespaApi:
        """Build an API whose control POST triggers a WebSocket-style push."""
        api = _make_api({"Heater": 0, "Filter": 0, "Bubble": 0})

        async def post_then_push(*args: Any, **kwargs: Any) -> None:
            api.merge_device_attrs(_DEVICE_ID, push)

        api._do_control_post = AsyncMock(  # type: ignore[method-assign]
            side_effect=post_then_push
        )
        return api

    async def test_target_temp_survives_push(self) -> None:
        """Target temperature has no optimistic overlay, so losing it is visible."""
        api = self._api_with_push_during_post({"Current_temperature": 31})

        await api.spa_set_target_temp(_DEVICE_ID, 38)

        attrs = _attrs(api)
        assert attrs["Temperature_setup"] == 38
        # The concurrent push is preserved too - neither write clobbers the other
        assert attrs["Current_temperature"] == 31

    async def test_filter_switch_survives_push(self) -> None:
        api = self._api_with_push_during_post({"Current_temperature": 31})

        await api.spa_set_filter(_DEVICE_ID, True)

        assert _attrs(api)["Filter"] == 1
        assert _attrs(api)["Current_temperature"] == 31

    async def test_heater_side_effect_survives_push(self) -> None:
        """The implied Filter=1 must survive as well as the Heater write."""
        api = self._api_with_push_during_post({"Current_temperature": 31})

        await api.spa_set_heat(_DEVICE_ID, True)

        attrs = _attrs(api)
        assert attrs["Heater"] == 1
        assert attrs["Filter"] == 1

    async def test_bubbles_survive_push(self) -> None:
        api = self._api_with_push_during_post({"Current_temperature": 31})

        await api.spa_set_bubbles(_DEVICE_ID, True)

        assert _attrs(api)["Bubble"] == 1

    async def test_local_write_wins_over_stale_push_of_same_field(self) -> None:
        """The command we just sent is newer than a push describing the old state."""
        api = self._api_with_push_during_post({"Filter": 0})

        await api.spa_set_filter(_DEVICE_ID, True)

        assert _attrs(api)["Filter"] == 1


class TestFetchDataConcurrency:
    """Device requests run concurrently, not one after another.

    Issued in sequence, a slow first device ate into the budget available to
    the rest, so an account with several spas could time out as a whole even
    though each request was within its own limit.
    """

    @staticmethod
    def _api_with_devices(*device_ids: str) -> WavespaApi:
        from unittest.mock import MagicMock

        api = WavespaApi(session=AsyncMock(), user_token="token", api_root="http://api")
        api.devices = {d: MagicMock() for d in device_ids}
        return api

    async def test_requests_overlap(self) -> None:
        """All requests are in flight before any of them completes."""
        api = self._api_with_devices("a", "b", "c")

        in_flight = 0
        peak = 0

        async def slow_get(url: str) -> dict[str, Any]:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return {"updated_at": 100, "attr": {"Heater": 1}}

        api._do_get = slow_get  # type: ignore[method-assign]
        await api.fetch_data()

        assert peak == 3, "requests were issued sequentially"

    async def test_every_device_is_still_cached(self) -> None:
        api = self._api_with_devices("a", "b")

        async def fake_get(url: str) -> dict[str, Any]:
            return {"updated_at": 100, "attr": {"Heater": 1}}

        api._do_get = fake_get  # type: ignore[method-assign]
        results = await api.fetch_data()

        assert set(results.devices) == {"a", "b"}

    async def test_results_are_matched_to_the_right_device(self) -> None:
        """Responses must not be zipped onto the wrong device."""
        api = self._api_with_devices("a", "b")

        async def fake_get(url: str) -> dict[str, Any]:
            did = url.removesuffix("/latest").rsplit("/", 1)[-1]
            return {"updated_at": 100, "attr": {"which": did}}

        api._do_get = fake_get  # type: ignore[method-assign]
        results = await api.fetch_data()

        assert results.devices["a"].attrs["which"] == "a"
        assert results.devices["b"].attrs["which"] == "b"


class TestPollSuppressionUsesMonotonicClock:
    """Polls are suppressed by a local monotonic window, not by clock comparison.

    The old rule compared the API's server-side `updated_at` against a locally
    stamped timestamp. Those are two different clocks, so a host running ahead
    of the Gizwits server discarded every poll for as long as the skew lasted -
    indefinitely on a host with no working NTP, silently leaving the WebSocket
    as the only source of state.

    The window has to cover WebSocket pushes as well as control writes: both
    carry data newer than any poll can, so a poll arriving just afterwards must
    not overwrite them with a staler server snapshot.
    """

    async def test_websocket_push_suppresses_poll(self) -> None:
        """A push is the device reporting directly; a poll must not undo it."""
        api = _make_api({"Filter": 0})
        api.merge_device_attrs(_DEVICE_ID, {"Filter": 1})
        assert api._local_write_is_recent(_DEVICE_ID) is True

    async def test_stale_poll_does_not_overwrite_a_fresh_push(self) -> None:
        api = _make_api({"Filter": 0})
        api.devices = {_DEVICE_ID: object()}  # type: ignore[dict-item]

        async def stale_get(url: str) -> dict[str, Any]:
            return {"updated_at": 1, "attr": {"Filter": 0}}

        api._do_get = stale_get  # type: ignore[method-assign]
        api.merge_device_attrs(_DEVICE_ID, {"Filter": 1})

        await api.fetch_data()

        assert _attrs(api)["Filter"] == 1

    async def test_recent_local_write_suppresses_poll(self) -> None:
        api = _make_api({"Filter": 0})
        await api.spa_set_filter(_DEVICE_ID, True)
        assert api._local_write_is_recent(_DEVICE_ID) is True

    async def test_window_expires(self) -> None:
        from custom_components.wavespa.wavespa import api as api_module

        api = _make_api({"Filter": 0})
        await api.spa_set_filter(_DEVICE_ID, True)

        written_at = api._fresh_writes[_DEVICE_ID]
        with patch.object(
            api_module,
            "monotonic",
            return_value=written_at + api_module._FRESH_WRITE_SETTLE_SECONDS + 1,
        ):
            assert api._local_write_is_recent(_DEVICE_ID) is False

    async def test_untouched_device_is_never_suppressed(self) -> None:
        api = _make_api()
        assert api._local_write_is_recent(_DEVICE_ID) is False

    async def test_host_clock_skew_does_not_suppress_polls(self) -> None:
        """A host clock hours ahead of the server must not block polling."""
        api = _make_api({"Filter": 0})
        # Cached state stamped far in the future, as a skewed host would.
        api._state_cache[_DEVICE_ID].timestamp = int(time.time()) + 86_400

        assert api._local_write_is_recent(_DEVICE_ID) is False


class TestMergeDeviceAttrs:
    """merge_device_attrs applies a partial delta without losing other fields."""

    async def test_merge_preserves_unmentioned_fields(self) -> None:
        api = _make_api({"Heater": 1, "Filter": 1, "Time_filter": 5000})
        api.merge_device_attrs(_DEVICE_ID, {"Heater": 0})
        attrs = _attrs(api)
        assert attrs["Heater"] == 0
        assert attrs["Filter"] == 1
        assert attrs["Time_filter"] == 5000

    async def test_merge_creates_entry_for_unknown_device(self) -> None:
        api = _make_api()
        api.merge_device_attrs("new_device", {"Heater": 1})
        assert api.cached_results().devices["new_device"].attrs == {"Heater": 1}

    async def test_merge_refreshes_timestamp(self) -> None:
        api = _make_api()
        assert api.cached_results().devices[_DEVICE_ID].timestamp == 1000
        api.merge_device_attrs(_DEVICE_ID, {"Heater": 1})
        assert api.cached_results().devices[_DEVICE_ID].timestamp > 1000

    async def test_cached_results_exposes_every_device(self) -> None:
        api = _make_api()
        api.merge_device_attrs("second_device", {"Heater": 1})
        assert set(api.cached_results().devices) == {_DEVICE_ID, "second_device"}
