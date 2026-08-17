"""Config flow for Wavespa integration."""

from __future__ import annotations

from collections.abc import Mapping
from logging import getLogger

from typing import Any

from aiohttp import ClientConnectionError
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .wavespa.api import (
    WavespaApi,
    WavespaIncorrectPasswordException,
    WavespaUserDoesNotExistException,
)
from .const import (
    CONFIG_VERSION,
    CONF_BUBBLES_WATTS,
    CONF_FILTER_WATTS,
    CONF_HEATER_WATTS,
    DEFAULT_BUBBLES_WATTS,
    DEFAULT_FILTER_WATTS,
    DEFAULT_HEATER_WATTS,
    CONF_API_ROOT,
    CONF_API_ROOT_EU,
    CONF_API_ROOT_US,
    CONF_LAN_HOST,
    CONF_PASSWORD,
    CONF_UID,
    CONF_USER_TOKEN,
    CONF_USER_TOKEN_EXPIRY,
    CONF_USERNAME,
    DOMAIN,
)

_LOGGER = getLogger(__name__)
_STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_API_ROOT): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=CONF_API_ROOT_EU, label="EU"),
                    selector.SelectOptionDict(value=CONF_API_ROOT_US, label="US"),
                ]
            )
        ),
    }
)

# Watts, as a plain number box. Bounded because a negative or absurd value
# would silently corrupt the Energy dashboard rather than fail visibly.
_WATTS_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0,
        max=10000,
        step=1,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="W",
    )
)

_STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_input(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Returns data to be stored in the config entry.
    """
    username = user_input[CONF_USERNAME]
    api_root = user_input[CONF_API_ROOT]
    session = async_get_clientsession(hass)
    # get_user_token applies its own timeout; wrapping it in a second one here
    # just duplicated the constant across two modules, where changing one would
    # silently leave the other disagreeing.
    token = await WavespaApi.get_user_token(
        session, username, user_input[CONF_PASSWORD], api_root
    )

    config_entry_data = dict(user_input)
    config_entry_data[CONF_USER_TOKEN] = token.user_token
    config_entry_data[CONF_USER_TOKEN_EXPIRY] = token.expiry
    config_entry_data[CONF_UID] = token.user_id
    return config_entry_data


class WavespaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for wavespa."""

    VERSION = CONFIG_VERSION

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=_STEP_USER_DATA_SCHEMA
            )

        errors = {}

        try:
            config_entry_data = await validate_input(self.hass, user_input)
        except WavespaUserDoesNotExistException:
            errors["base"] = "user_does_not_exist"
        except WavespaIncorrectPasswordException:
            errors["base"] = "incorrect_password"
        except ClientConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown_connection_error"
        else:
            # The account UID uniquely identifies the Wavespa account, so use
            # it to prevent the same account being added more than once.
            await self.async_set_unique_id(config_entry_data[CONF_UID])
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=user_input[CONF_USERNAME], data=config_entry_data
            )

        return self.async_show_form(
            step_id="user", data_schema=_STEP_USER_DATA_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> WavespaOptionsFlow:
        """Return the options flow for adjusting the wattage assumptions."""
        return WavespaOptionsFlow()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the credentials or API region without re-adding the entry.

        The README tells people to try the other region when their account is
        not found; without this, acting on that advice meant deleting the
        integration and losing all device and entity history.
        """
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is None:
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=self.add_suggested_values_to_schema(
                    _STEP_USER_DATA_SCHEMA,
                    {
                        CONF_USERNAME: reconfigure_entry.data[CONF_USERNAME],
                        CONF_API_ROOT: reconfigure_entry.data[CONF_API_ROOT],
                    },
                ),
            )

        errors = {}

        try:
            config_entry_data = await validate_input(self.hass, user_input)
        except WavespaUserDoesNotExistException:
            errors["base"] = "user_does_not_exist"
        except WavespaIncorrectPasswordException:
            errors["base"] = "incorrect_password"
        except ClientConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown_connection_error"
        else:
            # Reconfiguring must not silently repoint the entry at a different
            # account - that would strand every device already registered here.
            await self.async_set_unique_id(config_entry_data[CONF_UID])
            self._abort_if_unique_id_mismatch(reason="wrong_account")

            return self.async_update_reload_and_abort(
                reconfigure_entry, data_updates=config_entry_data
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _STEP_USER_DATA_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when stored credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm re-authentication by re-prompting for the password."""
        reauth_entry = self._get_reauth_entry()

        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=_STEP_REAUTH_DATA_SCHEMA,
                description_placeholders={
                    CONF_USERNAME: reauth_entry.data[CONF_USERNAME]
                },
            )

        errors = {}

        # Reuse the stored username and API location; only the password
        # is re-entered.
        validate_data = {
            CONF_USERNAME: reauth_entry.data[CONF_USERNAME],
            CONF_API_ROOT: reauth_entry.data[CONF_API_ROOT],
            CONF_PASSWORD: user_input[CONF_PASSWORD],
        }

        try:
            config_entry_data = await validate_input(self.hass, validate_data)
        except WavespaUserDoesNotExistException:
            errors["base"] = "user_does_not_exist"
        except WavespaIncorrectPasswordException:
            errors["base"] = "incorrect_password"
        except ClientConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown_connection_error"
        else:
            # The account must match the one already configured.
            await self.async_set_unique_id(config_entry_data[CONF_UID])
            self._abort_if_unique_id_mismatch(reason="wrong_account")

            return self.async_update_reload_and_abort(
                reauth_entry, data_updates=config_entry_data
            )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={CONF_USERNAME: reauth_entry.data[CONF_USERNAME]},
            errors=errors,
        )


class WavespaOptionsFlow(OptionsFlow):
    """Adjust the wattage assumptions, and optionally enable local control.

    The wattages are modelled, not metered: different models draw different
    amounts, and the EU and US variants differ on mains voltage alone. Because
    the numbers feed the Energy dashboard, a spa that does not match the
    defaults was silently accumulating wrong kWh with no supported way to
    correct it.

    The LAN host is blank by default. Local control needs an address that does
    not move, which is the user's job to arrange, so it is offered rather than
    assumed.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HEATER_WATTS,
                    default=options.get(CONF_HEATER_WATTS, DEFAULT_HEATER_WATTS),
                ): _WATTS_SELECTOR,
                vol.Required(
                    CONF_BUBBLES_WATTS,
                    default=options.get(CONF_BUBBLES_WATTS, DEFAULT_BUBBLES_WATTS),
                ): _WATTS_SELECTOR,
                vol.Required(
                    CONF_FILTER_WATTS,
                    default=options.get(CONF_FILTER_WATTS, DEFAULT_FILTER_WATTS),
                ): _WATTS_SELECTOR,
                # Optional, and empty is the supported way to turn local
                # control back off - vol.Optional with a "" default keeps the
                # box present but blank rather than making the user delete a
                # placeholder.
                vol.Optional(
                    CONF_LAN_HOST,
                    default=options.get(CONF_LAN_HOST, ""),
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
