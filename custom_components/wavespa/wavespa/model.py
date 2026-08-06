"""Wavespa API models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any


class WavespaDeviceType(Enum):
    """Wavespa device types."""

    WAVESPA_EU = "Wave_SPA_EU"
    WAVESPA_US = "Wave_SPA_US"
    UNKNOWN = "Unknown"

    @staticmethod
    def from_api_product_name(product_name: str) -> WavespaDeviceType:
        """Get the enum value based on the 'product_name' field in the API response."""

        if product_name == "Wave_SPA_EU":
            return WavespaDeviceType.WAVESPA_EU
        if product_name == "Wave_SPA_US":
            return WavespaDeviceType.WAVESPA_US
        return WavespaDeviceType.UNKNOWN


class TemperatureUnit(Enum):
    """Temperature units supported by the spa."""

    CELSIUS = auto()
    FAHRENHEIT = auto()


# Maximum raw "Time_filter" countdown value reported by the device, used to
# convert the raw value into a remaining-life percentage.
_TIME_FILTER_MAX = 10200


@dataclass
class WavespaDeviceStatus:
    """A snapshot of the status of a spa device."""

    timestamp: int
    attrs: dict[str, Any]

    @property
    def percent_filter(self) -> int | None:
        """Get the filter life as a percentage, derived from attrs.

        The device reports a raw "Time_filter" countdown in attrs; this
        converts it to a remaining-life percentage. Because it reads from
        attrs (which the coordinator merges across WebSocket deltas), the
        value persists even when a partial update omits Time_filter.
        """
        raw = self.attrs.get("Time_filter")
        if raw is None:
            return None
        percent = 100 - ((raw / _TIME_FILTER_MAX) * 100)
        return max(0, min(100, int(percent)))


@dataclass
class WavespaDevice:
    """A device under a user's account."""

    protocol_version: int
    device_id: str
    product_name: str
    alias: str
    mcu_soft_version: str
    mcu_hard_version: str
    wifi_soft_version: str
    wifi_hard_version: str
    is_online: bool
    ws_host: str = "m2m.gizwits.com"  # WebSocket hostname from bindings API
    ws_port: int = 8880  # WebSocket port from bindings API

    @property
    def device_type(self) -> WavespaDeviceType:
        """Get the derived device type."""
        return WavespaDeviceType.from_api_product_name(self.product_name)


@dataclass
class WavespaUserToken:
    """User authentication token, obtained (and ideally stored) following a successful login."""

    user_id: str
    user_token: str
    expiry: int
