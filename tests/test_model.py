"""Tests for derived properties on the API models.

``is_heating`` is the shared answer to "is the element actually drawing power
right now", used by both the thermostat's reported action and the wattage
estimate behind the Energy dashboard. The distinction it draws is not obvious
from the raw attrs: the spa sets Heater == 1 whenever heating is *enabled*,
including while it sits at temperature doing nothing at all.
"""

from typing import Any

import pytest

from custom_components.wavespa.wavespa.model import WavespaDeviceStatus, as_int


def _status(**attrs: Any) -> WavespaDeviceStatus:
    """Build a status carrying exactly the given attrs."""
    return WavespaDeviceStatus(timestamp=1000, attrs=dict(attrs))


class TestIsHeating:
    """The element only draws power while below the target."""

    def test_below_target_is_heating(self) -> None:
        status = _status(Heater=1, Current_temperature=30, Temperature_setup=40)
        assert status.is_heating is True

    def test_one_degree_below_target_is_heating(self) -> None:
        status = _status(Heater=1, Current_temperature=39, Temperature_setup=40)
        assert status.is_heating is True

    def test_at_target_is_not_heating(self) -> None:
        """Heating enabled but satisfied - the element has cycled off."""
        status = _status(Heater=1, Current_temperature=40, Temperature_setup=40)
        assert status.is_heating is False

    def test_above_target_is_not_heating(self) -> None:
        """An overshoot counts as reached, rather than heating indefinitely."""
        status = _status(Heater=1, Current_temperature=41, Temperature_setup=40)
        assert status.is_heating is False

    def test_heater_off_is_not_heating(self) -> None:
        status = _status(Heater=0, Current_temperature=30, Temperature_setup=40)
        assert status.is_heating is False

    def test_heater_off_at_target_is_not_heating(self) -> None:
        status = _status(Heater=0, Current_temperature=40, Temperature_setup=40)
        assert status.is_heating is False


class TestIsHeatingMissingReadings:
    """Any missing reading reports unknown rather than guessing."""

    def test_missing_heater(self) -> None:
        status = _status(Current_temperature=30, Temperature_setup=40)
        assert status.is_heating is None

    def test_missing_current_temperature(self) -> None:
        status = _status(Heater=1, Temperature_setup=40)
        assert status.is_heating is None

    def test_missing_target_temperature(self) -> None:
        status = _status(Heater=1, Current_temperature=30)
        assert status.is_heating is None

    def test_empty_attrs(self) -> None:
        assert _status().is_heating is None


class TestAsInt:
    """Every attribute read goes through this, so its edges matter."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, 0),
            (1, 1),
            ("0", 0),
            ("1", 1),
            (True, 1),
            (False, 0),
            (40.0, 40),
            ("40", 40),
            (None, None),
            ("", None),
            ("on", None),
            ([], None),
            ({}, None),
        ],
    )
    def test_coercion(self, value: Any, expected: int | None) -> None:
        assert as_int(value) == expected


class TestFlag:
    """flag() is the on/off reader shared by the switches, climate and sensors."""

    def test_zero_is_off(self) -> None:
        assert _status(Filter=0).flag("Filter") is False

    def test_string_zero_is_off(self) -> None:
        """The whole reason flag() exists: bool("0") is True."""
        assert _status(Filter="0").flag("Filter") is False

    def test_one_is_on(self) -> None:
        assert _status(Filter=1).flag("Filter") is True

    def test_string_one_is_on(self) -> None:
        assert _status(Filter="1").flag("Filter") is True

    def test_level_above_one_is_on(self) -> None:
        """Bubble reports a level on some models, not a simple on/off."""
        assert _status(Bubble=3).flag("Bubble") is True

    def test_missing_is_none(self) -> None:
        assert _status().flag("Filter") is None

    def test_unparseable_is_none(self) -> None:
        assert _status(Filter="on").flag("Filter") is None


class TestPercentFilter:
    """Filter life, derived from the raw Time_filter countdown."""

    def test_full(self) -> None:
        assert _status(Time_filter=0).percent_filter == 100

    def test_empty(self) -> None:
        assert _status(Time_filter=10200).percent_filter == 0

    def test_missing_is_none(self) -> None:
        assert _status().percent_filter is None

    def test_string_value_is_coerced(self) -> None:
        """Previously raised TypeError on a non-numeric type."""
        assert _status(Time_filter="10200").percent_filter == 0

    def test_unparseable_is_none_not_raise(self) -> None:
        assert _status(Time_filter="unknown").percent_filter is None

    def test_clamped_beyond_range(self) -> None:
        assert _status(Time_filter=99999).percent_filter == 0
        assert _status(Time_filter=-5).percent_filter == 100


class TestIsHeatingValueCoercion:
    """The API is inconsistent about types, so the readings are coerced."""

    def test_string_readings(self) -> None:
        status = _status(Heater="1", Current_temperature="30", Temperature_setup="40")
        assert status.is_heating is True

    def test_string_readings_at_target(self) -> None:
        """Strings must compare numerically, not lexically."""
        status = _status(Heater="1", Current_temperature="40", Temperature_setup="40")
        assert status.is_heating is False

    def test_boolean_heater(self) -> None:
        status = _status(Heater=True, Current_temperature=30, Temperature_setup=40)
        assert status.is_heating is True

    def test_string_zero_heater_is_not_heating(self) -> None:
        """bool("0") is True, so this needs numeric coercion to be correct."""
        status = _status(Heater="0", Current_temperature="30", Temperature_setup="40")
        assert status.is_heating is False

    def test_unparseable_heater_is_none(self) -> None:
        status = _status(Heater="yes", Current_temperature=30, Temperature_setup=40)
        assert status.is_heating is None

    def test_unparseable_temperature_is_none(self) -> None:
        status = _status(Heater=1, Current_temperature="warm", Temperature_setup=40)
        assert status.is_heating is None

    def test_zero_target_is_not_treated_as_missing(self) -> None:
        """A falsy-but-present reading is a value, not an absence."""
        status = _status(Heater=1, Current_temperature=30, Temperature_setup=0)
        assert status.is_heating is False
