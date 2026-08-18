"""Test wavespa config flow."""

import threading
from collections.abc import Generator
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
import pytest

from custom_components.wavespa.wavespa.model import WavespaUserToken
from custom_components.wavespa.const import (
    CONF_API_ROOT,
    CONF_BUBBLES_WATTS,
    CONF_FILTER_WATTS,
    CONF_HEATER_WATTS,
    CONF_LAN_HOST,
    CONF_API_ROOT_EU,
    CONF_PASSWORD,
    CONF_UID,
    CONF_USER_TOKEN,
    CONF_USER_TOKEN_EXPIRY,
    CONF_USERNAME,
    DOMAIN,
)

# Mock user input to the config flow
MOCK_USER_INPUT = {
    CONF_USERNAME: "test@example.org",
    CONF_PASSWORD: "P@asw0rd",
    CONF_API_ROOT: CONF_API_ROOT_EU,
}


# This fixture bypasses the actual setup of the integration
# since we only want to test the config flow. We test the
# actual functionality of the integration in other test modules.
@pytest.fixture(autouse=True)
def bypass_setup_fixture():
    """Prevent setup and unload.

    Config flow tests only exercise the flow logic, not the actual
    integration setup. Patching both prevents stray threads and teardown
    errors when HA tries to manage an entry that was never fully initialized.
    """
    with (
        patch(
            "custom_components.wavespa.async_setup_entry",
            return_value=True,
        ),
        patch(
            "custom_components.wavespa.async_unload_entry",
            return_value=True,
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def verify_cleanup() -> Generator[None]:
    """Override verify_cleanup to tolerate the _run_safe_shutdown_loop thread.

    The upstream fixture asserts no new threads exist after teardown, but
    shutdown_default_executor() spawns a short-lived daemon thread that
    races with this check. Config flow tests don't create real entities,
    so we just wait for any daemon threads to exit.
    """
    threads_before = frozenset(threading.enumerate())
    yield

    for thread in frozenset(threading.enumerate()) - threads_before:
        if thread.daemon:
            thread.join(timeout=2.0)


# Simiulate a successful config flow.
async def test_successful_config_flow(hass, bypass_get_data):
    """Test a successful config flow."""
    # Initialize a config flow
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    # Check that the config flow shows the user form as the first step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    # Mock an authentication call that provides a token to keep hold of
    token = WavespaUserToken("foo", "t0k3n", 123)
    with patch(
        "custom_components.wavespa.wavespa.api.WavespaApi.get_user_token",
        return_value=token,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=MOCK_USER_INPUT
        )

    expected_output = dict(MOCK_USER_INPUT)
    expected_output[CONF_UID] = token.user_id
    expected_output[CONF_USER_TOKEN] = token.user_token
    expected_output[CONF_USER_TOKEN_EXPIRY] = token.expiry

    # Check that the config flow is complete and a new entry is created with
    # the input data
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == MOCK_USER_INPUT[CONF_USERNAME]
    assert result["data"] == expected_output
    assert result["result"]


# Simulate an exception during the authentication process
async def test_failed_config_flow(hass, error_on_auth):
    """Test a failed config flow due to credential validation failure."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_INPUT
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "unknown_connection_error"}


async def test_options_flow_sets_wattages(hass):
    """The wattage assumptions can be corrected for a spa that differs."""
    from custom_components.wavespa.const import (
        CONF_BUBBLES_WATTS,
        CONF_FILTER_WATTS,
        CONF_HEATER_WATTS,
    )

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_UID: "uid",
        },
        version=2,
        entry_id="options_test",
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_HEATER_WATTS: 2400,
            CONF_BUBBLES_WATTS: 750,
            CONF_FILTER_WATTS: 40,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_HEATER_WATTS] == 2400
    assert config_entry.options[CONF_BUBBLES_WATTS] == 750
    assert config_entry.options[CONF_FILTER_WATTS] == 40


async def test_options_flow_sets_the_lan_host(hass):
    """Entering an address is how local control gets switched on."""
    from custom_components.wavespa.const import (
        CONF_BUBBLES_WATTS,
        CONF_FILTER_WATTS,
        CONF_HEATER_WATTS,
        CONF_LAN_HOST,
    )

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_UID: "uid",
        },
        version=2,
        entry_id="options_lan",
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_HEATER_WATTS: 1800,
            CONF_BUBBLES_WATTS: 600,
            CONF_FILTER_WATTS: 50,
            CONF_LAN_HOST: "192.0.2.10",
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_LAN_HOST] == "192.0.2.10"


async def test_options_flow_can_clear_the_lan_host(hass):
    """Emptying the box must actually turn local control off.

    The frontend omits an emptied optional field from the submitted data, so
    with the saved host as the schema *default* this resolved straight back to
    the old address - leaving no way to switch local control off from the UI.
    The host is a suggested value for exactly this reason.
    """
    from custom_components.wavespa.const import (
        CONF_BUBBLES_WATTS,
        CONF_FILTER_WATTS,
        CONF_HEATER_WATTS,
        CONF_LAN_HOST,
    )

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_UID: "uid",
        },
        options={CONF_LAN_HOST: "192.0.2.10"},
        version=2,
        entry_id="options_lan_clear",
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    # Exactly what the frontend sends when the box is emptied: the key absent.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_HEATER_WATTS: 1800,
            CONF_BUBBLES_WATTS: 600,
            CONF_FILTER_WATTS: 50,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert not config_entry.options.get(CONF_LAN_HOST)


def _suggested(result, key):
    """Read a field's suggested value out of a rendered form schema."""
    for marker in result["data_schema"].schema:
        if str(marker) == key:
            return (marker.description or {}).get("suggested_value")
    raise AssertionError(f"{key} is not in the form")


async def test_options_flow_does_not_search_unless_asked(hass):
    """Opening the form must not touch the network.

    A broadcast sweep takes seconds, and doing it on every visit would charge
    that to every user who never wanted local control - along with network
    traffic nobody asked a settings page for.
    """
    from unittest.mock import patch

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_UID: "uid",
        },
        version=2,
        entry_id="options_no_search",
    )
    config_entry.add_to_hass(hass)

    with patch("custom_components.wavespa.config_flow.async_discover") as discover:
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_HEATER_WATTS: 1800,
                CONF_BUBBLES_WATTS: 600,
                CONF_FILTER_WATTS: 50,
            },
        )

    discover.assert_not_called()


async def test_ticking_search_fills_in_the_address(hass):
    """The point of discovery: the user should not have to go and find an IP."""
    from unittest.mock import patch

    from custom_components.wavespa.const import CONF_SEARCH_FOR_SPA
    from custom_components.wavespa.lan.discovery import DiscoveredSpa

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_UID: "uid",
        },
        version=2,
        entry_id="options_search",
    )
    config_entry.add_to_hass(hass)

    found = [
        DiscoveredSpa(
            address="192.0.2.10",
            did="D" * 22,
            mac="a4e500112233",
            hardware_id="0402003A",
            product_key="pk",
        )
    ]

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    with patch(
        "custom_components.wavespa.config_flow.async_discover", return_value=found
    ) as discover:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_HEATER_WATTS: 2400,
                CONF_BUBBLES_WATTS: 600,
                CONF_FILTER_WATTS: 50,
                CONF_SEARCH_FOR_SPA: True,
            },
        )

    discover.assert_called_once()
    # Back on the form, not saved, with the address filled in...
    assert result["type"] == FlowResultType.FORM
    assert _suggested(result, CONF_LAN_HOST) == "192.0.2.10"
    # ...and the wattage the user had already typed still there.
    assert not config_entry.options


async def test_searching_and_finding_nothing_says_so(hass):
    """Plenty of routers block broadcasts, so silence needs explaining."""
    from unittest.mock import patch

    from custom_components.wavespa.const import CONF_SEARCH_FOR_SPA

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_UID: "uid",
        },
        version=2,
        entry_id="options_search_empty",
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    with patch("custom_components.wavespa.config_flow.async_discover", return_value=[]):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_HEATER_WATTS: 1800,
                CONF_BUBBLES_WATTS: 600,
                CONF_FILTER_WATTS: 50,
                CONF_SEARCH_FOR_SPA: True,
            },
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "no_spa_found"}


async def test_the_search_box_is_never_stored(hass):
    """It is an action, not a preference."""
    from custom_components.wavespa.const import CONF_SEARCH_FOR_SPA

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_UID: "uid",
        },
        version=2,
        entry_id="options_search_unstored",
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_HEATER_WATTS: 1800,
            CONF_BUBBLES_WATTS: 600,
            CONF_FILTER_WATTS: 50,
            CONF_SEARCH_FOR_SPA: False,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert CONF_SEARCH_FOR_SPA not in config_entry.options


async def test_searching_survives_a_broken_network(hass):
    """A sweep that raises must not break the settings page."""
    from unittest.mock import patch

    from custom_components.wavespa.const import CONF_SEARCH_FOR_SPA

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_UID: "uid",
        },
        version=2,
        entry_id="options_broken_net",
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    with patch(
        "custom_components.wavespa.config_flow.async_discover",
        side_effect=OSError("no route to host"),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_HEATER_WATTS: 1800,
                CONF_BUBBLES_WATTS: 600,
                CONF_FILTER_WATTS: 50,
                CONF_SEARCH_FOR_SPA: True,
            },
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "no_spa_found"}


async def test_options_flow_defaults_to_current_values(hass):
    """The form opens on the defaults rather than empty."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test@example.org",
            CONF_PASSWORD: "P@asw0rd",
            CONF_API_ROOT: CONF_API_ROOT_EU,
            CONF_UID: "uid",
        },
        version=2,
        entry_id="options_defaults",
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    # Submitting the form unchanged stores the defaults
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
