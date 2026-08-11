"""Tests for the coordinator's data update behaviour."""

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wavespa.wavespa.api import (
    WavespaApi,
    WavespaApiResults,
    WavespaTokenInvalidException,
)
from custom_components.wavespa.const import CONF_API_ROOT, CONF_API_ROOT_EU, DOMAIN
from custom_components.wavespa.coordinator import WavespaUpdateCoordinator


def _make_coordinator(
    hass: HomeAssistant,
) -> tuple[WavespaUpdateCoordinator, MagicMock]:
    """Build a coordinator over a mocked API."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_ROOT: CONF_API_ROOT_EU},
        entry_id="test",
    )
    api = MagicMock(spec=WavespaApi)
    return WavespaUpdateCoordinator(hass, config_entry, api), api


@pytest.mark.asyncio
async def test_invalid_token_triggers_reauth(hass: HomeAssistant):
    """Test an expired token asks the user to re-authenticate.

    Escaping as a generic exception would become UpdateFailed, which leaves
    the entities permanently unavailable with no way for the user to fix it.
    """
    coordinator, api = _make_coordinator(hass)
    api.fetch_data.side_effect = WavespaTokenInvalidException()

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_invalid_token_during_bindings_refresh_triggers_reauth(
    hass: HomeAssistant,
):
    """Test the bindings refresh doesn't swallow an auth failure.

    Its broad except is there to let a failed device-list refresh fall
    through to the status fetch, which an auth failure must not do.
    """
    coordinator, api = _make_coordinator(hass)
    api.refresh_bindings.side_effect = WavespaTokenInvalidException()

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    # The auth failure short-circuits the update
    api.fetch_data.assert_not_called()


@pytest.mark.asyncio
async def test_bindings_refresh_failure_still_serves_devices(hass: HomeAssistant):
    """Test a non-auth bindings failure falls through to the status fetch."""
    coordinator, api = _make_coordinator(hass)
    api.refresh_bindings.side_effect = TimeoutError("upstream is slow")
    expected = WavespaApiResults({})
    api.fetch_data.return_value = expected

    assert await coordinator._async_update_data() is expected
