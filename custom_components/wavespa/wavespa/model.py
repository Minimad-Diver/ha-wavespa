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


# Full service life of the filter, in whatever units "Time_filter" is reported
# in. Time_filter counts *up* from zero as the filter is used, so this is the
# value it climbs towards, not a starting point it counts down from.
_TIME_FILTER_MAX = 10200


def as_int(value: Any) -> int | None:
    """Coerce an attribute to int, returning None if it isn't numeric.

    The API is not consistent about types, and the difference matters: a
    string "0" is truthy in Python, so reading an on/off flag with bool()
    reports a spa that is off as running. Everything that interprets an
    attribute goes through here so the whole integration agrees.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class WavespaDeviceStatus:
    """A snapshot of the status of a spa device."""

    timestamp: int
    attrs: dict[str, Any]

    @property
    def percent_filter(self) -> int | None:
        """Get the remaining filter life as a percentage, derived from attrs.

        "Time_filter" counts up from zero as the filter is used, towards
        _TIME_FILTER_MAX, so the remaining life is the inverse of how far it
        has climbed. A low raw value means a nearly new filter.

        (Earlier comments here called it a countdown, which would have made
        this calculation backwards. Confirmed against a real device: it counts
        up, and the arithmetic below is right.)

        Because it reads from attrs (which the coordinator merges across
        WebSocket deltas), the value persists even when a partial update omits
        Time_filter.
        """
        raw = as_int(self.attrs.get("Time_filter"))
        if raw is None:
            return None
        percent = 100 - ((raw / _TIME_FILTER_MAX) * 100)
        return max(0, min(100, int(percent)))

    def flag(self, name: str) -> bool | None:
        """Return an on/off attribute as a bool, or None if absent or unusable.

        Anything non-zero counts as on, so this also covers the attributes that
        report a level rather than a simple on/off (e.g. Bubble).
        """
        value = as_int(self.attrs.get(name))
        return None if value is None else value != 0

    @property
    def is_heating(self) -> bool | None:
        """Return whether the heating element is actually drawing power.

        The spa reports Heater == 1 whenever heating is *enabled*, including
        while it sits at temperature doing nothing, so the target has to be
        checked too. Returns None if any of the three readings are missing or
        cannot be read as numbers.
        """
        heat_on = self.flag("Heater")
        target = as_int(self.attrs.get("Temperature_setup"))
        current = as_int(self.attrs.get("Current_temperature"))
        if heat_on is None or target is None or current is None:
            return None
        # Use >= for "reached" so an overshoot (e.g. 41 °C against a 40 °C
        # target) still counts, rather than heating indefinitely.
        return heat_on and current < target


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
    # Identifies the product, not this unit, and is the lookup key for the
    # datapoint definition the LAN transport decodes status with. Empty if the
    # bindings response omitted it, which only costs that device local
    # control - the cloud transport needs nothing from it.
    product_key: str = ""

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
