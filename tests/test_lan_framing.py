"""Tests for the Gizwits LAN transport envelope.

Pure logic - no device involved. Includes the real discovery reply captured
from a Wave Spa Garda, so the parser is pinned against actual hardware output
rather than only against frames this code built itself.
"""

from __future__ import annotations

import pytest

from custom_components.wavespa.lan.framing import (
    CMD_DISCOVER_RESPONSE,
    PROTOCOL_VERSION,
    Frame,
    FramingError,
    decode_varlen,
    encode_varlen,
    pack,
    split_stream,
    unpack,
)

# Captured from a real spa. The identifying fields (device id, MAC) are
# replaced with same-length placeholders; the framing is untouched.
REAL_DISCOVERY_REPLY = bytes.fromhex(
    "000000037a0000040016"
    + b"AAAAAAAAAAAAAAAAAAAAAA".hex()
    + "0006"
    + "aabbccddeeff"
    + "0008"
    + b"0402003A".hex()
    + "0020"
    + b"747be354e00449e799883a966d0c9cbd".hex()
    + "000000000000000065756170692e67697a776974732e636f6d3a383000342e312e32003030303030303030"
)


class TestVarlen:
    """Lengths are 7 bits per byte with a continuation flag."""

    @pytest.mark.parametrize("value", [0, 1, 127, 128, 129, 255, 300, 16383, 16384])
    def test_round_trip(self, value: int) -> None:
        encoded = encode_varlen(value)
        decoded, offset = decode_varlen(encoded)
        assert decoded == value
        assert offset == len(encoded)

    def test_small_values_are_a_single_byte(self) -> None:
        assert encode_varlen(127) == b"\x7f"

    def test_the_boundary_needs_two(self) -> None:
        assert encode_varlen(128) == b"\x80\x01"

    def test_negative_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            encode_varlen(-1)

    def test_truncated_length_raises(self) -> None:
        """Every byte has the continuation bit set, so it never terminates."""
        with pytest.raises(FramingError):
            decode_varlen(b"\x80\x80\x80")

    def test_implausible_length_raises(self) -> None:
        with pytest.raises(FramingError):
            decode_varlen(b"\x80" * 10)


class TestPackUnpack:
    """A packet we build must be one we can read back."""

    def test_round_trip(self) -> None:
        frame = unpack(pack(0x0090, b"\x01\x02\x03"))
        assert frame.cmd == 0x0090
        assert frame.payload == b"\x01\x02\x03"
        assert frame.flag == 0

    def test_empty_payload(self) -> None:
        assert unpack(pack(0x0015)).payload == b""

    def test_version_prefix_is_three(self) -> None:
        """Version 4 gets no reply from real hardware - see the constant."""
        assert pack(0x0015).startswith(b"\x00\x00\x00\x03")
        assert PROTOCOL_VERSION == b"\x00\x00\x00\x03"

    def test_long_payload_uses_a_multi_byte_length(self) -> None:
        raw = pack(0x0091, b"\xaa" * 500)
        assert unpack(raw).payload == b"\xaa" * 500

    def test_command_must_fit_in_two_bytes(self) -> None:
        with pytest.raises(ValueError):
            pack(0x10000)

    def test_unsolicited_flag_is_surfaced(self) -> None:
        """How a pushed status is told apart from an answer to a request."""
        assert unpack(pack(0x0091, b"\x00", flag=1)).is_unsolicited is True
        assert unpack(pack(0x0091, b"\x00", flag=0)).is_unsolicited is False


class TestUnpackRejectsBadInput:
    """A packet we cannot read is a protocol problem, not silence."""

    def test_wrong_prefix(self) -> None:
        with pytest.raises(FramingError, match="expected prefix"):
            unpack(b"\x00\x00\x00\x04\x03\x00\x00\x03")

    def test_body_shorter_than_declared(self) -> None:
        with pytest.raises(FramingError, match="claims"):
            unpack(b"\x00\x00\x00\x03\x20\x00\x00\x90")

    def test_body_too_short_for_a_header(self) -> None:
        with pytest.raises(FramingError, match="too short"):
            unpack(b"\x00\x00\x00\x03\x02\x00\x00")

    def test_empty_input(self) -> None:
        with pytest.raises(FramingError):
            unpack(b"")


class TestRealDiscoveryReply:
    """Parsed against output captured from actual hardware."""

    def test_parses(self) -> None:
        frame = unpack(REAL_DISCOVERY_REPLY)
        assert frame.cmd == CMD_DISCOVER_RESPONSE

    def test_carries_the_product_key(self) -> None:
        frame = unpack(REAL_DISCOVERY_REPLY)
        assert b"747be354e00449e799883a966d0c9cbd" in frame.payload

    def test_carries_the_regional_endpoint(self) -> None:
        """Matches the API root the config entry already stores."""
        assert b"euapi.gizwits.com" in unpack(REAL_DISCOVERY_REPLY).payload


class TestSplitStream:
    """TCP has no message boundaries, so reads split and merge packets."""

    def test_two_packets_in_one_read(self) -> None:
        frames, rest = split_stream(pack(0x0015) + pack(0x0016))
        assert [f.cmd for f in frames] == [0x0015, 0x0016]
        assert rest == b""

    def test_a_partial_packet_is_kept_for_next_time(self) -> None:
        whole = pack(0x0091, b"\x01\x02\x03")
        frames, rest = split_stream(whole[:-2])
        assert frames == []
        assert rest == whole[:-2]

    def test_a_whole_packet_followed_by_a_partial(self) -> None:
        first, second = pack(0x0015), pack(0x0091, b"\xff" * 10)
        frames, rest = split_stream(first + second[:5])
        assert [f.cmd for f in frames] == [0x0015]
        assert rest == second[:5]

    def test_resuming_completes_the_packet(self) -> None:
        """The remainder prepended to the next read must parse cleanly."""
        whole = pack(0x0091, b"\x01\x02\x03")
        _, rest = split_stream(whole[:-2])
        frames, rest = split_stream(rest + whole[-2:])
        assert [f.cmd for f in frames] == [0x0091]
        assert rest == b""

    def test_garbage_stops_the_scan(self) -> None:
        frames, rest = split_stream(b"garbage")
        assert frames == []
        assert rest == b"garbage"

    def test_frames_are_comparable(self) -> None:
        """Frozen dataclass, so tests can compare whole frames."""
        assert Frame(cmd=1, payload=b"a") == Frame(cmd=1, payload=b"a")
