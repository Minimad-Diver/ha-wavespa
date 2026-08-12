"""Tests for derived properties on the API models.

``is_heating`` is the shared answer to "is the element actually drawing power
right now", used by both the thermostat's reported action and the wattage
estimate behind the Energy dashboard. The distinction it draws is not obvious
from the raw attrs: the spa sets Heater == 1 whenever heating is *enabled*,
including while it sits at temperature doing nothing at all.
"""

from typing import Any

from custom_components.wavespa.wavespa.model import WavespaDeviceStatus


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

    def test_zero_target_is_not_treated_as_missing(self) -> None:
        """A falsy-but-present reading is a value, not an absence."""
        status = _status(Heater=1, Current_temperature=30, Temperature_setup=0)
        assert status.is_heating is False
