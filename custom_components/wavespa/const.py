"""Constants for the wavespa integration."""

from enum import Enum

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


class Icon(str, Enum):
    """Icon styles."""

    BUBBLES = "mdi:chart-bubble"
    FILTER = "mdi:image-filter-tilt-shift"
    HARDWARE = "mdi:chip"
    PROTOCOL = "mdi:protocol"
    SOFTWARE = "mdi:application-braces"
