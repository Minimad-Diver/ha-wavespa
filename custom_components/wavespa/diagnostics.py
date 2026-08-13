"""Diagnostics support for the wavespa integration.

The device attributes are reverse-engineered and vary by model, so the single
most useful thing a bug report can carry is the raw attr dictionary. Without
this, getting it means talking the reporter through enabling debug logging.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD, CONF_UID, CONF_USER_TOKEN, CONF_USERNAME
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
    "device_id",
    "mac",
    "passcode",
    "product_key",
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
        devices.append(
            {
                "device_id": device_id,
                "product_name": device.product_name,
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
            },
            "devices": devices,
        },
        TO_REDACT,
    )
