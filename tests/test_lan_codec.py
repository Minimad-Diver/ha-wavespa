"""Tests for the datapoint codec.

Driven by the real product definition for the Wave Spa Garda, bundled as a
fixture, so these pin the codec against the actual product rather than a
hand-made approximation of it.

The decisive case is `test_real_observed_state_round_trips`: the exact
attributes a real spa reported, encoded and decoded back unchanged. Getting a
byte offset wrong on a writable datapoint means writing to the wrong field on
someone's hardware, so the schema is checked against reality rather than
assumed.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from custom_components.wavespa.lan.codec import (
    CodecError,
    DatapointSchema,
    decode_attrs,
    encode_attrs,
)

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "datapoint_wave_spa.json"

# Exactly what a real spa reported, sitting above its target temperature.
OBSERVED_ATTRS: dict[str, Any] = {
    "Bubble": 0,
    "Current_temperature": 26,
    "Filter": 1,
    "Fp": 0,
    "Heater": 1,
    "Overtime_filter": 0,
    "Superheat": 0,
    "Temp1": 0,
    "Temp2": 0,
    "Temp3": 0,
    "Temperature_setup": 24,
    "Time_filter": 22,
    "Undercooling": 0,
    "bit1": 0,
    "bit2": 0,
}

# 05 18 00 00 00 1a 00 16 00
#  |  |  \--------/  \---/  \-- byte 8: alerts, all clear
#  |  |      |         \------- bytes 6-7: Time_filter = 22
#  |  |      \----------------- bytes 2-4: Temp1-3, then byte 5 = 26 current
#  |  \------------------------ byte 1: Temperature_setup = 24
#  \--------------------------- byte 0: Heater + Filter set
OBSERVED_PAYLOAD = bytes.fromhex("05180000001a001600")


@pytest.fixture(name="schema")
def schema_fixture() -> DatapointSchema:
    return DatapointSchema(json.loads(_FIXTURE.read_text(encoding="utf8")))


class TestSchema:
    """Parsed from the definition the API serves."""

    def test_product_key(self, schema: DatapointSchema) -> None:
        assert schema.product_key == "747be354e00449e799883a966d0c9cbd"

    def test_every_datapoint_is_understood(self, schema: DatapointSchema) -> None:
        """All 15 are types the codec handles, so none is silently skipped."""
        assert len(schema.datapoints) == 15

    def test_payload_is_nine_bytes(self, schema: DatapointSchema) -> None:
        assert schema.payload_size == 9

    def test_alerts_match_the_manufacturer(self, schema: DatapointSchema) -> None:
        """The evidence the Alerts sensor is built on."""
        assert set(schema.alert_names) == {
            "Overtime_filter",
            "Superheat",
            "Undercooling",
        }

    def test_current_temperature_is_read_only(self, schema: DatapointSchema) -> None:
        assert "Current_temperature" not in schema.writable_names

    def test_target_temperature_is_writable(self, schema: DatapointSchema) -> None:
        assert "Temperature_setup" in schema.writable_names

    def test_bit_packed_positions(self, schema: DatapointSchema) -> None:
        heater = schema.by_name("Heater")
        assert heater is not None
        assert (heater.byte_offset, heater.bit_offset, heater.data_type) == (
            0,
            0,
            "bool",
        )

    def test_time_filter_is_a_two_byte_value(self, schema: DatapointSchema) -> None:
        """Confirms _TIME_FILTER_MAX = 10200 is a uint16, not a guess."""
        time_filter = schema.by_name("Time_filter")
        assert time_filter is not None
        assert time_filter.data_type == "uint16"
        assert time_filter.width == 2

    def test_unknown_name_returns_none(self, schema: DatapointSchema) -> None:
        assert schema.by_name("NoSuchField") is None

    def test_definition_without_datapoints_is_rejected(self) -> None:
        with pytest.raises(CodecError):
            DatapointSchema({"entities": []})

    def test_unsupported_types_are_skipped_not_guessed(self) -> None:
        """A wrong offset on a writable field would be written to hardware."""
        schema = DatapointSchema(
            {
                "entities": [
                    {
                        "attrs": [
                            {
                                "name": "Good",
                                "data_type": "uint8",
                                "position": {"byte_offset": 0},
                                "type": "status_writable",
                            },
                            {
                                "name": "Exotic",
                                "data_type": "binary",
                                "position": {"byte_offset": 1},
                                "type": "status_writable",
                            },
                        ]
                    }
                ]
            }
        )
        assert [dp.name for dp in schema.datapoints] == ["Good"]


class TestDecode:
    """Payload in, cloud-shaped attributes out."""

    def test_real_observed_state_round_trips(self, schema: DatapointSchema) -> None:
        """The whole basis for trusting the schema against real hardware."""
        assert encode_attrs(schema, OBSERVED_ATTRS) == OBSERVED_PAYLOAD
        assert decode_attrs(schema, OBSERVED_PAYLOAD) == OBSERVED_ATTRS

    def test_names_match_the_cloud_api(self, schema: DatapointSchema) -> None:
        """Why LAN state can feed the existing cache untouched."""
        assert set(decode_attrs(schema, OBSERVED_PAYLOAD)) == set(OBSERVED_ATTRS)

    def test_bits_are_read_from_the_right_positions(
        self, schema: DatapointSchema
    ) -> None:
        # byte 0: Heater bit 0, Bubble bit 1, Filter bit 2
        attrs = decode_attrs(schema, bytes([0b00000110]) + bytes(8))
        assert (attrs["Heater"], attrs["Bubble"], attrs["Filter"]) == (0, 1, 1)

    def test_multi_byte_values_are_big_endian(self, schema: DatapointSchema) -> None:
        payload = bytearray(9)
        payload[6:8] = (10200).to_bytes(2, "big")
        assert decode_attrs(schema, bytes(payload))["Time_filter"] == 10200

    def test_short_payload_is_an_error(self, schema: DatapointSchema) -> None:
        """Guessing which fields arrived would show wrong values to a user."""
        with pytest.raises(CodecError, match="needs 9"):
            decode_attrs(schema, bytes(8))

    def test_longer_payload_is_tolerated(self, schema: DatapointSchema) -> None:
        """Trailing bytes are not our business; a short one is."""
        decoded = decode_attrs(schema, OBSERVED_PAYLOAD + b"\xff\xff")
        assert decoded == OBSERVED_ATTRS


class TestEncode:
    """Attributes in, payload out."""

    def test_unknown_attributes_are_ignored(self, schema: DatapointSchema) -> None:
        """Cloud responses may carry fields this product does not have."""
        assert encode_attrs(schema, {"NoSuchField": 1}) == bytes(9)

    def test_partial_attributes_leave_the_rest_clear(
        self, schema: DatapointSchema
    ) -> None:
        payload = encode_attrs(schema, {"Heater": 1})
        assert payload[0] == 0b00000001
        assert payload[1:] == bytes(8)

    def test_string_values_are_coerced(self, schema: DatapointSchema) -> None:
        """The API is loose about types; int() keeps "0" from meaning on."""
        assert encode_attrs(schema, {"Heater": "0"}) == bytes(9)
        assert encode_attrs(schema, {"Heater": "1"})[0] == 1

    def test_every_writable_field_survives_a_round_trip(
        self, schema: DatapointSchema
    ) -> None:
        """Each writable field gets a distinct value, so an offset collision
        between two of them would show up as a wrong value coming back."""
        written = {}
        for index, name in enumerate(schema.writable_names):
            datapoint = schema.by_name(name)
            assert datapoint is not None
            if datapoint.data_type == "bool":
                written[name] = 1
            elif datapoint.data_type == "uint8":
                written[name] = 200 - index  # distinct, and inside uint8
            else:
                written[name] = 9000 + index

        decoded = decode_attrs(schema, encode_attrs(schema, written))

        for name, value in written.items():
            assert decoded[name] == value, f"{name} did not survive the round trip"
