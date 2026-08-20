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
import logging
import pathlib
from typing import Any

import pytest

from custom_components.wavespa.lan.codec import (
    REQUIRED_DATAPOINTS,
    CodecError,
    DatapointSchema,
    decode_attrs,
    encode_attrs,
    encode_write,
    require_datapoints,
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


def _definition_with_an_exotic_type() -> dict[str, Any]:
    """A definition holding one datapoint the codec cannot safely read."""
    return {
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
        """The definition's 10200 ceiling is a uint16 field, not a guess.

        That ceiling is what the field can carry, not the filter's service
        life - the counter stops at 10080. _TIME_FILTER_MAX in model.py is the
        latter, and deliberately not this number.
        """
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
        schema = DatapointSchema(_definition_with_an_exotic_type())
        assert [dp.name for dp in schema.datapoints] == ["Good"]

    def test_a_skipped_datapoint_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Otherwise the only symptom is an entity that never gets a value.

        Nothing connects that back to the product definition, so a model this
        codec cannot fully read would look like a bug in the integration.
        """
        with caplog.at_level(logging.WARNING):
            DatapointSchema(_definition_with_an_exotic_type())

        assert "Exotic" in caplog.text
        assert "binary" in caplog.text

    def test_a_nameless_datapoint_is_skipped_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A datapoint with no name cannot be looked up, so it is unusable."""
        with caplog.at_level(logging.WARNING):
            schema = DatapointSchema(
                {
                    "entities": [
                        {
                            "attrs": [
                                {
                                    "name": "Good",
                                    "data_type": "uint8",
                                    "position": {"byte_offset": 0},
                                },
                                {
                                    "data_type": "uint8",
                                    "position": {"byte_offset": 1},
                                },
                            ]
                        }
                    ]
                }
            )

        assert [dp.name for dp in schema.datapoints] == ["Good"]
        assert "no name" in caplog.text

    def test_a_datapoint_without_a_byte_offset_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            DatapointSchema(
                {
                    "entities": [
                        {
                            "attrs": [
                                {
                                    "name": "Good",
                                    "data_type": "uint8",
                                    "position": {"byte_offset": 0},
                                },
                                {"name": "Placeless", "data_type": "uint8"},
                            ]
                        }
                    ]
                }
            )

        assert "Placeless" in caplog.text
        assert "byte offset" in caplog.text


class TestRequiredDatapoints:
    """Whether a product's definition can drive this integration's entities."""

    def test_the_real_product_satisfies_them(self, schema: DatapointSchema) -> None:
        require_datapoints(schema)  # must not raise

    def test_nothing_is_missing_from_the_real_product(
        self, schema: DatapointSchema
    ) -> None:
        assert schema.missing(REQUIRED_DATAPOINTS) == ()

    def test_missing_reports_names_sorted(self, schema: DatapointSchema) -> None:
        assert schema.missing(frozenset({"Zebra", "Aardvark"})) == (
            "Aardvark",
            "Zebra",
        )

    def test_a_renamed_datapoint_is_refused(self) -> None:
        """The failure this exists to catch.

        Such a definition decodes perfectly well - into names nothing looks
        up - so without this check every entity sits at unknown and the log
        says nothing at all.
        """
        schema = DatapointSchema(
            {
                "name": "Some_Other_Spa",
                "entities": [
                    {
                        "attrs": [
                            {
                                "name": "WaterTemp",
                                "data_type": "uint8",
                                "position": {"byte_offset": 0},
                            }
                        ]
                    }
                ],
            }
        )

        with pytest.raises(CodecError, match="Some_Other_Spa"):
            require_datapoints(schema)

    def test_the_message_names_only_what_is_missing(
        self, schema: DatapointSchema
    ) -> None:
        with pytest.raises(CodecError, match="Nope") as raised:
            require_datapoints(schema, frozenset({"Heater", "Filter", "Nope"}))

        assert "Heater" not in str(raised.value)

    def test_alerts_are_not_required(self, schema: DatapointSchema) -> None:
        """The Alerts sensor tolerates their absence, so a product without
        them is still worth talking to over the LAN."""
        assert not REQUIRED_DATAPOINTS & {
            "Overtime_filter",
            "Superheat",
            "Undercooling",
        }


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


class TestEncodeWrite:
    """The attr_flags + attr_vals half of a 0x93 write.

    Every expected byte string here was verified against a real spa: the
    device accepted it and the datapoint changed, with nothing else moving.
    """

    def test_a_boolean(self, schema: DatapointSchema) -> None:
        """Bubble is flag bit 1, and byte 0 bit 1 of the values."""
        assert encode_write(schema, {"Bubble": 1}).hex() == "00020200000000"

    def test_a_boolean_cleared(self, schema: DatapointSchema) -> None:
        """Still flagged, so the device is told to set it to zero rather than
        left to infer it from an absent flag."""
        assert encode_write(schema, {"Bubble": 0}).hex() == "00020000000000"

    def test_a_multi_byte_value(self, schema: DatapointSchema) -> None:
        """Temperature_setup is flag bit 6, at byte 1 of the values.

        The case that proves values sit at their schema byte offsets rather
        than being packed together in flag order.
        """
        assert encode_write(schema, {"Temperature_setup": 23}).hex() == (
            "00400017000000"
        )

    def test_several_at_once(self, schema: DatapointSchema) -> None:
        """The combination a filter-off has to send.

        The spa will not stop the pump while heating is enabled - Filter=0 on
        its own is acknowledged and ignored - so both travel together.
        """
        assert encode_write(schema, {"Filter": 0, "Heater": 0}).hex() == (
            "00050000000000"
        )

    def test_the_flag_field_is_two_bytes_for_this_product(
        self, schema: DatapointSchema
    ) -> None:
        """One byte per eight writable datapoints; this product has ten."""
        assert len(schema.writable_names) == 10
        assert len(encode_write(schema, {"Bubble": 1})) == 2 + 5

    def test_writing_nothing_is_refused(self, schema: DatapointSchema) -> None:
        with pytest.raises(CodecError, match="at least one"):
            encode_write(schema, {})

    def test_an_unknown_datapoint_is_refused(self, schema: DatapointSchema) -> None:
        with pytest.raises(CodecError, match="no datapoint named"):
            encode_write(schema, {"NoSuchField": 1})

    def test_a_read_only_datapoint_is_refused(self, schema: DatapointSchema) -> None:
        """Silently dropping it would report a command as sent that the spa
        was never asked to carry out."""
        with pytest.raises(CodecError, match="read-only"):
            encode_write(schema, {"Current_temperature": 20})

    def test_an_alert_datapoint_is_refused(self, schema: DatapointSchema) -> None:
        with pytest.raises(CodecError, match="read-only"):
            encode_write(schema, {"Superheat": 0})

    def test_a_value_that_does_not_fit_is_refused(
        self, schema: DatapointSchema
    ) -> None:
        with pytest.raises(CodecError, match="does not fit"):
            encode_write(schema, {"Temperature_setup": 999})

    def test_a_write_round_trips_through_the_decoder(
        self, schema: DatapointSchema
    ) -> None:
        """The value block is laid out the same way a status payload is, so
        decoding one back gives the values that were written."""
        body = encode_write(schema, {"Heater": 1, "Temperature_setup": 40})
        values = body[2:]  # past the two flag bytes

        decoded = decode_attrs(schema, values + bytes(4))

        assert decoded["Heater"] == 1
        assert decoded["Temperature_setup"] == 40


class TestEncode:
    """Attributes in, payload out."""

    def test_unknown_attributes_are_rejected(self, schema: DatapointSchema) -> None:
        """Once this feeds the write path, silently dropping a field would
        report a command as sent that the spa never acted on.

        Callers holding a cloud attribute dictionary that may carry fields
        this product lacks should filter it themselves, so that dropping one
        is a decision rather than an accident.
        """
        with pytest.raises(CodecError, match="NoSuchField"):
            encode_attrs(schema, {"NoSuchField": 1})

    def test_a_filtered_cloud_dictionary_still_encodes(
        self, schema: DatapointSchema
    ) -> None:
        """The supported way to handle attributes of uncertain provenance."""
        cloud_attrs = {"Heater": 1, "SomeFutureField": 7}
        known = {k: v for k, v in cloud_attrs.items() if schema.by_name(k)}

        assert encode_attrs(schema, known)[0] == 1

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
