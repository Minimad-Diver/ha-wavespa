"""Diagnostics support for the wavespa integration.

The device attributes are reverse-engineered and vary by model, so the single
most useful thing a bug report can carry is the raw attr dictionary. Without
this, getting it means talking the reporter through enabling debug logging.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_LAN_HOST,
    CONF_PASSWORD,
    CONF_UID,
    CONF_USER_TOKEN,
    CONF_USERNAME,
)
from .coordinator import WavespaConfigEntry

# Credentials and account identifiers. The device attrs themselves carry no
# personal data - they are temperatures and on/off flags - so they are reported
# verbatim, which is the entire point of this file.
TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_USER_TOKEN,
    CONF_UID,
    # Device identifiers, redacted for the same reason api.py masks them
    # before logging: people paste diagnostics into public issues.
    #
    # product_key is deliberately NOT here. It identifies the product, not the
    # unit - every spa of the same model shares it, and Gizwits serves it
    # unauthenticated - so it gives away nothing about the owner. It is also
    # the lookup key for the datapoint layout a status payload is decoded
    # with, which makes it the single most useful field when local control
    # reports the wrong values or nothing at all.
    "device_id",
    "mac",
    "passcode",
    # The spa's address on the user's own network. Not a credential, but it
    # describes their LAN, and the options blob it lives in is dumped whole.
    CONF_LAN_HOST,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WavespaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    api = coordinator.api

    devices = []
    for device_id, device in api.devices.items():
        status = coordinator.data.devices.get(device_id) if coordinator.data else None
        last_push = coordinator.last_websocket_update(device_id)
        last_lan = coordinator.last_lan_update(device_id)
        devices.append(
            {
                "device_id": device_id,
                # Same treatment as the WebSocket timestamp, and the first
                # thing worth knowing about a "local control isn't working"
                # report: None means this spa has never delivered over the LAN.
                "last_lan_update": (
                    dt_util.utc_from_timestamp(last_lan).isoformat()
                    if last_lan is not None
                    else None
                ),
                # Rendered rather than reported as a raw epoch float, which is
                # unreadable in a pasted diagnostics dump. None means this spa
                # has pushed nothing since setup.
                "last_websocket_update": (
                    dt_util.utc_from_timestamp(last_push).isoformat()
                    if last_push is not None
                    else None
                ),
                "product_name": device.product_name,
                # Reported in full - see TO_REDACT. It identifies the model,
                # not the spa, and it is what decides how a status payload is
                # decoded, so a "local control shows nothing" report is far
                # easier to act on with it attached.
                "product_key": device.product_key,
                "device_type": device.device_type.value,
                "protocol_version": device.protocol_version,
                "is_online": device.is_online,
                "mcu_soft_version": device.mcu_soft_version,
                "mcu_hard_version": device.mcu_hard_version,
                "wifi_soft_version": device.wifi_soft_version,
                "wifi_hard_version": device.wifi_hard_version,
                "ws_host": device.ws_host,
                "ws_port": device.ws_port,
                "status": {
                    "timestamp": status.timestamp,
                    "attrs": status.attrs,
                    "is_heating": status.is_heating,
                    "percent_filter": status.percent_filter,
                }
                if status is not None
                else None,
            }
        )

    return async_redact_data(
        {
            "entry": {
                "version": entry.version,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "coordinator": {
                "last_update_success": coordinator.last_update_success,
                "update_interval": str(coordinator.update_interval),
                "websocket_connected": (
                    coordinator.websocket.is_connected
                    if coordinator.websocket is not None
                    else None
                ),
                # None distinguishes "local control is switched off" from
                # "configured but not connecting", which are different bugs.
                "lan_connected": (
                    coordinator.lan.is_connected
                    if coordinator.lan is not None
                    else None
                ),
                # What the wifi module says about itself, read over the LAN.
                # Worth reporting because discovery cannot run at all under
                # containerised Home Assistant, so for those installs this is
                # the only way the hardware id ever becomes visible.
                "lan_module": (
                    {
                        "module": coordinator.lan.module_info.module,
                        "hardware_id": coordinator.lan.module_info.hardware_id,
                        "product_key": coordinator.lan.module_info.product_key,
                    }
                    if coordinator.lan is not None
                    and coordinator.lan.module_info is not None
                    else None
                ),
            },
            "devices": devices,
        },
        TO_REDACT,
    )
