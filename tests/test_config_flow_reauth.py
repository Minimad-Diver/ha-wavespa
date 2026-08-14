"""Tests for the reauthentication and reconfiguration flows.

Both flows were entirely uncovered: 43 of config_flow.py's 101 statements, with
neither method ever executed by a test.

The case that matters most is the account-mismatch guard both share:

    await self.async_set_unique_id(config_entry_data[CONF_UID])
    self._abort_if_unique_id_mismatch(reason="wrong_account")

Two lines that stop a different Wavespa account's credentials silently
repointing an existing entry. Without them every device and entity already
registered would be stranded, and their history attached to a spa that no
longer owns them.
"""

import threading
from collections.abc import Generator
from unittest.mock import patch

import pytest
from aiohttp import ClientConnectionError
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wavespa.const import (
    CONF_API_ROOT,
    CONF_API_ROOT_EU,
    CONF_API_ROOT_US,
    CONF_PASSWORD,
    CONF_UID,
    CONF_USER_TOKEN,
    CONF_USER_TOKEN_EXPIRY,
    CONF_USERNAME,
    DOMAIN,
)
from custom_components.wavespa.wavespa.api import (
    WavespaIncorrectPasswordException,
    WavespaUserDoesNotExistException,
)
from custom_components.wavespa.wavespa.model import WavespaUserToken

_EXISTING_UID = "uid-original"

_STORED_DATA = {
    CONF_USERNAME: "test@example.org",
    CONF_PASSWORD: "P@asw0rd",
    CONF_API_ROOT: CONF_API_ROOT_EU,
    CONF_USER_TOKEN: "old-token",
    CONF_USER_TOKEN_EXPIRY: 1234,
    CONF_UID: _EXISTING_UID,
}

# Each failure the flows distinguish, and the message key it must produce.
_ERROR_CASES = [
    (WavespaIncorrectPasswordException(), "incorrect_password"),
    (WavespaUserDoesNotExistException(), "user_does_not_exist"),
    (ClientConnectionError("unreachable"), "cannot_connect"),
    (Exception("something else"), "unknown_connection_error"),
]


@pytest.fixture(autouse=True)
def bypass_setup_fixture():
    """Exercise the flow logic without setting the integration up for real."""
    with (
        patch("custom_components.wavespa.async_setup_entry", return_value=True),
        patch("custom_components.wavespa.async_unload_entry", return_value=True),
    ):
        yield


@pytest.fixture(autouse=True)
def verify_cleanup() -> Generator[None]:
    """Tolerate the short-lived shutdown thread, as test_config_flow.py does."""
    threads_before = frozenset(threading.enumerate())
    yield

    for thread in frozenset(threading.enumerate()) - threads_before:
        if thread.daemon:
            thread.join(timeout=2.0)


def _add_entry(hass, entry_id: str) -> MockConfigEntry:
    """Add a configured entry belonging to account _EXISTING_UID."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=dict(_STORED_DATA),
        unique_id=_EXISTING_UID,
        version=2,
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)
    return entry


def _token(uid: str = _EXISTING_UID) -> WavespaUserToken:
    return WavespaUserToken(user_id=uid, user_token="new-token", expiry=999)


def _patch_login(**kwargs):
    """Patch the login call both flows validate against."""
    return patch(
        "custom_components.wavespa.wavespa.api.WavespaApi.get_user_token", **kwargs
    )


class TestReauth:
    """Fires automatically when a stored token stops working."""

    async def test_form_names_the_account(self, hass) -> None:
        entry = _add_entry(hass, "reauth_form")

        result = await entry.start_reauth_flow(hass)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        # Home Assistant adds its own "name" placeholder, so check the one the
        # integration supplies rather than the whole mapping.
        placeholders = result["description_placeholders"]
        assert placeholders[CONF_USERNAME] == _STORED_DATA[CONF_USERNAME]

    async def test_success_updates_the_stored_token(self, hass) -> None:
        entry = _add_entry(hass, "reauth_ok")
        result = await entry.start_reauth_flow(hass)

        with _patch_login(return_value=_token()):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={CONF_PASSWORD: "new-password"}
            )
            await hass.async_block_till_done()

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data[CONF_USER_TOKEN] == "new-token"
        # Username and region are reused rather than re-entered
        assert entry.data[CONF_USERNAME] == _STORED_DATA[CONF_USERNAME]
        assert entry.data[CONF_API_ROOT] == CONF_API_ROOT_EU

    async def test_another_account_is_refused(self, hass) -> None:
        """The guard this whole file exists for.

        Accepting a different account's credentials would strand every device
        already registered against this entry.
        """
        entry = _add_entry(hass, "reauth_mismatch")
        result = await entry.start_reauth_flow(hass)

        with _patch_login(return_value=_token(uid="uid-somebody-else")):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={CONF_PASSWORD: "their-password"}
            )
            await hass.async_block_till_done()

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "wrong_account"
        # The original account is untouched
        assert entry.data[CONF_UID] == _EXISTING_UID
        assert entry.data[CONF_USER_TOKEN] == "old-token"

    @pytest.mark.parametrize(("raised", "expected"), _ERROR_CASES)
    async def test_errors_keep_the_user_on_the_form(
        self, hass, raised, expected
    ) -> None:
        entry = _add_entry(hass, f"reauth_{expected}")
        result = await entry.start_reauth_flow(hass)

        with _patch_login(side_effect=raised):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={CONF_PASSWORD: "whatever"}
            )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        assert result["errors"] == {"base": expected}


class TestReconfigure:
    """Invoked deliberately, e.g. to switch API region without losing history."""

    async def test_form_is_prefilled(self, hass) -> None:
        """The README's "try the other endpoint" advice should not mean retyping."""
        entry = _add_entry(hass, "reconfigure_form")

        result = await entry.start_reconfigure_flow(hass)

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        suggested = {
            key.schema: key.description["suggested_value"]
            for key in result["data_schema"].schema
            if isinstance(key.description, dict)
            and "suggested_value" in key.description
        }
        assert suggested[CONF_USERNAME] == _STORED_DATA[CONF_USERNAME]
        assert suggested[CONF_API_ROOT] == CONF_API_ROOT_EU

    async def test_can_change_the_api_region(self, hass) -> None:
        """Switching EU to US keeps the entry, its devices and their history."""
        entry = _add_entry(hass, "reconfigure_ok")
        result = await entry.start_reconfigure_flow(hass)

        with _patch_login(return_value=_token()):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={
                    CONF_USERNAME: _STORED_DATA[CONF_USERNAME],
                    CONF_PASSWORD: "P@asw0rd",
                    CONF_API_ROOT: CONF_API_ROOT_US,
                },
            )
            await hass.async_block_till_done()

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_API_ROOT] == CONF_API_ROOT_US
        assert entry.data[CONF_USER_TOKEN] == "new-token"

    async def test_another_account_is_refused(self, hass) -> None:
        """Same guard as reauth: the account cannot be swapped underneath."""
        entry = _add_entry(hass, "reconfigure_mismatch")
        result = await entry.start_reconfigure_flow(hass)

        with _patch_login(return_value=_token(uid="uid-somebody-else")):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={
                    CONF_USERNAME: "someone@example.org",
                    CONF_PASSWORD: "their-password",
                    CONF_API_ROOT: CONF_API_ROOT_EU,
                },
            )
            await hass.async_block_till_done()

        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "wrong_account"
        assert entry.data[CONF_UID] == _EXISTING_UID
        assert entry.data[CONF_API_ROOT] == CONF_API_ROOT_EU

    @pytest.mark.parametrize(("raised", "expected"), _ERROR_CASES)
    async def test_errors_keep_the_user_on_the_form(
        self, hass, raised, expected
    ) -> None:
        entry = _add_entry(hass, f"reconfigure_{expected}")
        result = await entry.start_reconfigure_flow(hass)

        with _patch_login(side_effect=raised):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={
                    CONF_USERNAME: _STORED_DATA[CONF_USERNAME],
                    CONF_PASSWORD: "wrong",
                    CONF_API_ROOT: CONF_API_ROOT_EU,
                },
            )

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reconfigure"
        assert result["errors"] == {"base": expected}


class TestUserStepErrors:
    """The three named failures on initial setup.

    Only the catch-all branch was covered before, so these could have been
    wired to the wrong message keys without anything noticing.
    """

    @pytest.mark.parametrize(("raised", "expected"), _ERROR_CASES[:3])
    async def test_specific_errors(self, hass, raised, expected) -> None:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

        with _patch_login(side_effect=raised):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={
                    CONF_USERNAME: "test@example.org",
                    CONF_PASSWORD: "P@asw0rd",
                    CONF_API_ROOT: CONF_API_ROOT_EU,
                },
            )

        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {"base": expected}
