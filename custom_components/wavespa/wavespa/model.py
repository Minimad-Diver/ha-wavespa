"""Wavespa API models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from math import floor
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
#
# 10080 minutes is exactly seven days of filtering, and is where the counter
# stops: a real spa read 10080 with Overtime_filter raised, then still 10080
# fifteen minutes later with the pump running. The product definition's
# uint_spec says max 10200, but that is the largest value the field can carry,
# not the service life - taking it literally left the sensor reporting 1% on a
# filter the spa had already declared expired, so it could never reach 0.
_TIME_FILTER_MAX = 10080

# The datapoints the manufacturer types as "alert" in the product definition
# (product_key 747be354e00449e799883a966d0c9cbd, byte 8 bits 0-2), rather than
# as status_writable or status_readonly like everything else. Their Chinese
# descriptions in that definition are the authority on what each means:
# Overtime_filter is 过滤超时, "filter timeout"; Superheat is 过热, "overheat";
# Undercooling is 过冷, "overcool".
#
# Named explicitly, not pattern-matched. An earlier sensor searched for
# system_err*, E##, earth and error - Bestway names this hardware has never
# sent - so it could not turn on at all. Guessing at attribute meanings is
# also how the Heater switch went wrong, so this list changes only when the
# product definition says it should.
ALERT_ATTRS = ("Overtime_filter", "Superheat", "Undercooling")


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
        # Rounded to nearest, which is what the manufacturer's own app does.
        # Established from two readings taken either side of the point where
        # the app changed: it still said 99% at a count in the 140s, and 98%
        # at 159. Truncating flips at 101 and rounding up not until 202, so
        # both are ruled out and only nearest fits.
        #
        # Truncating also meant a filter reset a minute ago read 99% rather
        # than 100%, which is what prompted looking at this at all.
        #
        # floor(x + 0.5) rather than round(), because round() breaks exact
        # halves towards even and there are about twenty counts where the
        # percentage lands on one. Nothing turns on those, but half-up is what
        # "rounded" is taken to mean and is likelier to be what the app does.
        #
        # The cost, accepted knowingly: 0% now arrives about fifty minutes
        # before the spa raises Overtime_filter, so the two no longer coincide
        # exactly. Agreeing with the app the user is comparing against is worth
        # more than that alignment, and the Alerts sensors report expiry
        # properly.
        percent = 100 - ((raw / _TIME_FILTER_MAX) * 100)
        return max(0, min(100, floor(percent + 0.5)))

    @property
    def filter_minutes_remaining(self) -> float | None:
        """Return the filtering time left before the spa calls the filter spent.

        Minutes of *filtering*, not minutes from now. Time_filter only
        advances while the pump is running, so a spa that filters for eight
        hours a day takes about three weeks to spend seven days of filter
        life. The entity's name says so, because a duration that looks like a
        countdown to a date would be read as one.

        None when Time_filter is missing or unreadable, matching
        percent_filter: not knowing is not the same as nothing left.
        """
        raw = as_int(self.attrs.get("Time_filter"))
        if raw is None:
            return None
        # A count is a minute. Measured across a fresh filter on a live spa:
        # 113 counts in 6759 seconds, or 59.8s each, which with the counter's
        # one-count resolution is sixty. An earlier note here put it at 65s;
        # that was wrong, and it made a full filter 182 hours instead of the
        # 168 that 10080 minutes plainly is.
        return float(max(0, _TIME_FILTER_MAX - raw))

    def alerts(self) -> dict[str, bool]:
        """Return each alert this spa reports, active or not.

        Attributes the spa does not send are omitted rather than reported as
        clear, so the result reflects what this model actually has.

        Read through flag() for the same reason as everywhere else: bool("0")
        is True, and a spa sending its flags as text would otherwise report a
        fault that is not there.
        """
        return {
            name: self.flag(name) is True for name in ALERT_ATTRS if name in self.attrs
        }

    def active_alerts(self) -> tuple[str, ...]:
        """Return the names of the alerts that are currently raised."""
        return tuple(name for name, active in self.alerts().items() if active)

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
