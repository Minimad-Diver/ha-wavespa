"""The wavespa integration."""

from __future__ import annotations

from datetime import datetime, timedelta
from logging import getLogger

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .lan import CodecError, DatapointSchema, GizwitsLanSession
from .wavespa.api import WavespaApi, WavespaAuthException
from .wavespa.websocket import GizwitsWebSocket
from .const import (
    CONFIG_VERSION,
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
from .coordinator import WavespaConfigEntry, WavespaUpdateCoordinator

_LOGGER = getLogger(__name__)

# Unique ID suffixes of entities this integration used to create and no longer
# does. Home Assistant keeps registry entries for entities that stop being
# provided, so without this they linger as "unavailable" forever and the user
# has to delete each one by hand.
#
# Only ever add to this list, with one exception: an entry must be removed if
# the entity is ever reinstated, or setup would delete it moments before the
# platform recreates it - on every restart, discarding the user's history and
# customisations each time. That is why _spa_has_error is absent: the alerts
# sensor reuses its unique ID.
_OBSOLETE_UNIQUE_ID_SUFFIXES = (
    # Removed in cddb92a: WaveSpa hardware has no power button, so a switch
    # writing the Heater field duplicated the thermostat.
    "_Heater",
    # Removed for #47: superseded by sw_version/hw_version on the device.
    "_mcu_soft_version",
    "_mcu_hard_version",
    "_wifi_soft_version",
    "_wifi_hard_version",
)


def _async_remove_obsolete_entities(
    hass: HomeAssistant, entry: WavespaConfigEntry
) -> None:
    """Delete registry entries for entities this integration no longer creates."""
    registry = er.async_get(hass)

    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not registry_entry.unique_id.endswith(_OBSOLETE_UNIQUE_ID_SUFFIXES):
            continue

        _LOGGER.debug(
            "Removing obsolete entity %s from the registry", registry_entry.entity_id
        )
        registry.async_remove(registry_entry.entity_id)


_PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def _async_setup_lan(
    hass: HomeAssistant,
    entry: WavespaConfigEntry,
    coordinator: WavespaUpdateCoordinator,
    api: WavespaApi,
) -> None:
    """Start a local-network session if one is configured and possible.

    Opt-in and best-effort throughout: every way this can fail leaves the
    cloud transport running and logs why, because local control is an
    enhancement and losing it must never cost the user their spa entities.

    Confirmed against real hardware that the LAN announces state changes
    unprompted, so this feeds the same cache the WebSocket does rather than
    needing a polling design of its own.
    """
    host = str(entry.options.get(CONF_LAN_HOST) or "").strip()
    if not host:
        _LOGGER.debug("No LAN host configured, local control is off")
        return

    if len(api.devices) != 1:
        # A single configured address cannot be attributed to one of several
        # spas, and guessing would show one spa's readings on another - a far
        # worse outcome than simply not offering local control. Multi-spa
        # support needs discovery, which can match a device by its DID.
        _LOGGER.warning(
            "LAN host is configured but the account has %d devices; local "
            "control supports one and has been left off",
            len(api.devices),
        )
        return

    device_id, device = next(iter(api.devices.items()))

    if not device.product_key:
        _LOGGER.warning(
            "No product_key for device %s, so its datapoint layout cannot be "
            "fetched; local control is off",
            device_id,
        )
        return

    try:
        definition = await api.get_datapoint_definition(device.product_key)
        schema = DatapointSchema(definition)
        session = GizwitsLanSession(
            host,
            schema,
            coordinator.handle_lan_update,
            connect_callback=coordinator.set_lan_active,
            disconnect_callback=coordinator.handle_lan_disconnect,
        )
    except CodecError as err:
        # The product's definition cannot drive our entities - a different
        # model, or a renamed datapoint. Permanent, so say so plainly.
        _LOGGER.warning("Local control is not available for this spa: %s", err)
        return
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.warning("Could not set up local control, staying on the cloud: %s", err)
        return

    coordinator.set_lan_device(device_id)
    coordinator.lan = session

    # Tracked, so HA cancels it on unload. An untracked task would keep
    # reconnecting after a reload and hold one of the spa's few connection
    # slots for a session nothing is listening to.
    entry.async_create_background_task(
        hass,
        session.async_run(),
        f"{DOMAIN}-{entry.entry_id}-lan",
    )
    _LOGGER.debug("LAN session to %s started for device %s", host, device_id)


async def async_setup_entry(hass: HomeAssistant, entry: WavespaConfigEntry) -> bool:
    """Set up wavespa from a config entry."""
    username = str(entry.data.get(CONF_USERNAME))
    password = str(entry.data.get(CONF_PASSWORD))
    api_root = str(entry.data.get(CONF_API_ROOT))
    user_token = str(entry.data.get(CONF_USER_TOKEN))
    user_token_expiry = entry.data.get(CONF_USER_TOKEN_EXPIRY)

    if not isinstance(user_token_expiry, int):
        user_token_expiry = 0

    session = async_get_clientsession(hass)

    # Check for an auth token
    # If we have one that expires within 30 days, refresh it
    # Also refresh if UID is missing (for WebSocket support)
    expiry_cutoff = (datetime.now() + timedelta(days=30)).timestamp()
    uid = entry.data.get(CONF_UID)

    if user_token and expiry_cutoff < user_token_expiry and uid:
        _LOGGER.debug("Reusing existing access token")
    else:
        if not uid:
            _LOGGER.debug("UID missing, fetching new token to enable WebSocket")
        else:
            _LOGGER.debug("Requesting a new auth token")

        try:
            token = await WavespaApi.get_user_token(
                session, username, password, api_root
            )
        except WavespaAuthException as ex:
            # Credentials are no longer valid (e.g. password changed) -
            # retrying won't help, so prompt the user to re-authenticate.
            _LOGGER.error("Authentication failed while refreshing token: %s", ex)
            raise ConfigEntryAuthFailed from ex
        except Exception as ex:  # pylint: disable=broad-except
            # Transient problem (network, server) - let HA retry setup later.
            _LOGGER.error("Failed to refresh API token: %s", ex)
            raise ConfigEntryNotReady from ex
        user_token = token.user_token
        user_token_expiry = token.expiry
        uid = token.user_id

        new_config_data = {
            CONF_USER_TOKEN: user_token,
            CONF_USER_TOKEN_EXPIRY: user_token_expiry,
            CONF_UID: uid,
        }

        hass.config_entries.async_update_entry(
            entry, data={**entry.data, **new_config_data}
        )

    api = WavespaApi(session, user_token, api_root)
    coordinator = WavespaUpdateCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    # Initialize WebSocket for real-time updates
    # uid variable is set above (either from config or from token refresh)
    ws_client = None

    if uid:
        try:
            # Get WebSocket endpoint from first device
            if api.devices:
                first_device = next(iter(api.devices.values()))

                ws_client = GizwitsWebSocket(
                    uid=uid,
                    token=user_token,
                    ws_host=first_device.ws_host,
                    ws_port=first_device.ws_port,
                    update_callback=coordinator.handle_websocket_update,
                    disconnect_callback=coordinator.handle_websocket_disconnect,
                    connect_callback=coordinator.set_websocket_active,
                    online_status_callback=coordinator.handle_websocket_online_status,
                )

                # Run the supervisor for as long as the entry is loaded. Using
                # a tracked background task means HA cancels it on unload -
                # an untracked task would keep reconnecting after a reload,
                # leaving a zombie client behind for every reload.
                entry.async_create_background_task(
                    hass,
                    ws_client.async_run(),
                    f"{DOMAIN}-{entry.entry_id}-websocket",
                )

                # Polling deliberately stays at its default rate here. The
                # client's connect callback slows it down once the feed is
                # actually live; reducing it eagerly would leave a spa that
                # never manages to connect on 5-minute polling with no push
                # updates to make up the difference.
                _LOGGER.debug("WebSocket client initialized")
            else:
                _LOGGER.warning("No devices found, WebSocket not initialized")
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning(
                "Failed to setup WebSocket, falling back to polling: %s", ex
            )
    else:
        _LOGGER.debug("No UID in config, WebSocket disabled (polling only)")

    coordinator.websocket = ws_client

    await _async_setup_lan(hass, entry, coordinator, api)

    _async_remove_obsolete_entities(hass, entry)

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WavespaConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data

    # Cleanup WebSocket connection before tearing down platforms
    if coordinator.websocket is not None:
        await coordinator.websocket.disconnect()
        _LOGGER.debug("WebSocket client disconnected")

    # Close the LAN session explicitly rather than relying on the background
    # task being cancelled. The spa has a small fixed pool of connection slots
    # and reclaims one only on a clean close or a timeout, so a reload that
    # abandoned the socket would burn a slot each time.
    if coordinator.lan is not None:
        await coordinator.lan.disconnect()
        _LOGGER.debug("LAN session disconnected")

    unload_ok: bool = await hass.config_entries.async_unload_platforms(
        entry, _PLATFORMS
    )

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: WavespaConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: WavespaConfigEntry) -> bool:
    """Migrate old config versions to the latest.

    Steps are cumulative and each upgrades to the next version, so an entry
    several versions behind is carried all the way forward. Written this way
    deliberately: the previous version handled `entry.version == 1` and failed
    everything else, which was unreachable while the current version was 2 but
    would have rejected every existing v2 entry the moment a v3 was added.
    """

    _LOGGER.debug("Migrating from version %s", entry.version)

    if entry.version > CONFIG_VERSION:
        # Downgrades can't be handled - the entry was written by a newer
        # version of the integration than this one.
        _LOGGER.error(
            "Config entry version %s is newer than the supported version %s",
            entry.version,
            CONFIG_VERSION,
        )
        return False

    data = {**entry.data}

    if entry.version < 2:
        # API root needs to be set
        # In version 1, this was hard coded to the EU endpoint
        data[CONF_API_ROOT] = CONF_API_ROOT_EU

    if entry.version < CONFIG_VERSION:
        hass.config_entries.async_update_entry(entry, data=data, version=CONFIG_VERSION)
        _LOGGER.debug("Migration to version %s successful", CONFIG_VERSION)

    return True
