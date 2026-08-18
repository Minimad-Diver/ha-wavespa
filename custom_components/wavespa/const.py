"""Constants for the wavespa integration."""

DOMAIN = "wavespa"

# Current config entry schema version. Lives here rather than only on the
# config flow so async_migrate_entry can bound its migration steps without
# importing the flow.
CONFIG_VERSION = 2
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_API_ROOT = "apiroot"
CONF_API_ROOT_EU = "https://euapi.gizwits.com"
CONF_API_ROOT_US = "https://usapi.gizwits.com"
CONF_USER_TOKEN = "user_token"
CONF_USER_TOKEN_EXPIRY = "user_token_expiry"
CONF_UID = "uid"
GIZWITS_APP_ID = "78a879318939402b9c70819d918ef8ed"

# Options: assumed power draw of each load, in watts. These feed the
# estimated power and energy sensors, which are modelled rather than
# metered, so they need to be adjustable per spa.
CONF_HEATER_WATTS = "heater_watts"
CONF_BUBBLES_WATTS = "bubbles_watts"
CONF_FILTER_WATTS = "filter_watts"

DEFAULT_HEATER_WATTS = 1800
DEFAULT_BUBBLES_WATTS = 600
DEFAULT_FILTER_WATTS = 50

# Options: the spa's address on the local network. Empty means the LAN
# transport is off and the integration behaves exactly as it always has -
# local control is opt-in, because it needs a fixed address the user has to
# arrange (a DHCP reservation) and a wrong one would just log failures.
CONF_LAN_HOST = "lan_host"

# A transient tick-box on the options form, never stored. Discovery only runs
# when it is asked for: a broadcast sweep takes seconds, and doing it whenever
# the form opens would charge that to every user who never wanted local
# control, along with network traffic they did not ask a settings page for.
CONF_SEARCH_FOR_SPA = "search_for_spa"
