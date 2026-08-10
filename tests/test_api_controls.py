"""Tests for the optimistic state-cache writes in wavespa/api.py.

Each ``spa_set_*`` method POSTs a single field to the API and then updates the
local state cache to match, because a GET immediately after a POST still
returns the old values. Some methods also update *other* cached attributes to
reflect side effects the spa applies on its own.

These tests pin down which side effects each setter is allowed to have:
- the heater cannot run without the filter pump, so they move together
- the bubbles are independent of both
"""

from typing import Any, cast
from unittest.mock import AsyncMock

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
