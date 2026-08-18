"""Tests for LAN discovery.

Identifiers here are synthetic. A real reply carries the spa's DID and MAC,
which identify the specific unit, so a captured one is not committed - the
field layout is pinned instead, and the parser was checked against real
hardware by hand.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from custom_components.wavespa.lan.discovery import (
    _REQUEST,
    DISCOVERY_PORT,
    DiscoveredSpa,
    _DiscoveryProtocol,
    async_discover,
    parse_discovery_reply,
)
from custom_components.wavespa.lan.framing import (
    CMD_DISCOVER_RESPONSE,
    CMD_PONG,
    pack,
)

# What a real reply looks like: four length-prefixed fields, then a mixture of
# padding and free-form strings that does not continue the same pattern.
_DID = b"K" * 22
_MAC = bytes.fromhex("a4e500112233")
_HARDWARE = b"0402003A"
_PRODUCT_KEY = b"747be354e00449e799883a966d0c9cbd"
_TRAILING = bytes(8) + b"euapi.gizwits.com:80" + bytes(3) + b"4.1.2"


def _reply(
    did: bytes = _DID,
    mac: bytes = _MAC,
    hardware: bytes = _HARDWARE,
    product_key: bytes = _PRODUCT_KEY,
    trailing: bytes = _TRAILING,
) -> bytes:
    payload = b"".join(
        len(field).to_bytes(2, "big") + field
        for field in (did, mac, hardware, product_key)
    )
    return pack(CMD_DISCOVER_RESPONSE, payload + trailing)


class TestParseReply:
    """Turning a datagram into an identity."""

    def test_all_four_fields_are_read(self) -> None:
        spa = parse_discovery_reply("192.0.2.10", _reply())

        assert spa == DiscoveredSpa(
            address="192.0.2.10",
            did="K" * 22,
            mac="a4e500112233",
            hardware_id="0402003A",
            product_key="747be354e00449e799883a966d0c9cbd",
        )

    def test_trailing_content_is_ignored(self) -> None:
        """Real replies carry padding and strings after the fields we want."""
        assert parse_discovery_reply("192.0.2.10", _reply(trailing=b"")) == (
            parse_discovery_reply("192.0.2.10", _reply())
        )

    def test_the_mac_is_hex_not_raw_bytes(self) -> None:
        """Formatting is the caller's business, which keeps this HA-free."""
        spa = parse_discovery_reply("192.0.2.10", _reply())
        assert spa is not None
        assert spa.mac == "a4e500112233"

    def test_another_command_is_ignored(self) -> None:
        assert parse_discovery_reply("192.0.2.10", pack(CMD_PONG)) is None

    def test_an_unparsable_datagram_is_ignored(self) -> None:
        """Something else entirely on the port must not raise."""
        assert parse_discovery_reply("192.0.2.10", b"not a packet") is None

    def test_a_truncated_field_is_ignored(self) -> None:
        payload = b"\x00\x16" + b"K" * 5  # claims 22 bytes, carries 5
        assert parse_discovery_reply("192.0.2.10", pack(0x0004, payload)) is None

    def test_a_zero_length_field_is_ignored(self) -> None:
        assert parse_discovery_reply("192.0.2.10", _reply(did=b"")) is None

    def test_non_ascii_identifiers_are_ignored(self) -> None:
        """Another Gizwits product may lay its reply out differently."""
        assert parse_discovery_reply("192.0.2.10", _reply(did=b"\xff" * 22)) is None

    def test_a_short_reply_is_ignored(self) -> None:
        assert parse_discovery_reply("192.0.2.10", pack(0x0004, b"\x00")) is None

    def test_a_failure_is_logged_rather_than_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            parse_discovery_reply("192.0.2.10", b"rubbish")

        assert "192.0.2.10" in caplog.text


class TestProtocol:
    """The datagram collector."""

    def test_a_reply_is_kept(self) -> None:
        protocol = _DiscoveryProtocol()
        protocol.datagram_received(b"payload", ("192.0.2.10", DISCOVERY_PORT))
        assert protocol.replies == {"192.0.2.10": b"payload"}

    def test_our_own_broadcast_is_ignored(self) -> None:
        """The host loops it back; it is not a device answering."""
        protocol = _DiscoveryProtocol()
        protocol.datagram_received(_REQUEST, ("192.0.2.99", DISCOVERY_PORT))
        assert protocol.replies == {}

    def test_the_first_reply_per_address_wins(self) -> None:
        """A device answering both attempts is one device, not two."""
        protocol = _DiscoveryProtocol()
        protocol.datagram_received(b"first", ("192.0.2.10", DISCOVERY_PORT))
        protocol.datagram_received(b"second", ("192.0.2.10", DISCOVERY_PORT))
        assert protocol.replies == {"192.0.2.10": b"first"}

    def test_a_socket_error_does_not_raise(self) -> None:
        """One ICMP error must not abandon the sweep."""
        _DiscoveryProtocol().error_received(OSError("unreachable"))


class FakeTransport:
    """Records what was broadcast, and answers on cue."""

    def __init__(self, answers: dict[str, bytes] | None = None) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False
        self.unreachable: set[str] = set()
        self._answers = answers or {}
        self.protocol: _DiscoveryProtocol | None = None

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        if addr[0] in self.unreachable:
            raise OSError("network is down")
        self.sent.append((data, addr))
        # A real device answers while the sweep is still listening.
        assert self.protocol is not None
        for address, reply in self._answers.items():
            self.protocol.datagram_received(reply, (address, DISCOVERY_PORT))

    def close(self) -> None:
        self.closed = True

    def factory(self) -> Any:
        async def _open(protocol: _DiscoveryProtocol) -> tuple[Any, None]:
            self.protocol = protocol
            return self, None

        return _open


class TestDiscoverySweep:
    """Sending, waiting and collecting."""

    async def test_no_broadcast_addresses_finds_nothing(self) -> None:
        assert await async_discover([]) == []

    async def test_every_address_is_tried_on_every_attempt(self) -> None:
        transport = FakeTransport()

        await async_discover(
            ["192.0.2.255", "198.51.100.255"],
            timeout=0,
            attempts=2,
            endpoint_factory=transport.factory(),
        )

        assert len(transport.sent) == 4
        assert {addr[0] for _, addr in transport.sent} == {
            "192.0.2.255",
            "198.51.100.255",
        }
        assert all(addr[1] == DISCOVERY_PORT for _, addr in transport.sent)
        assert all(data == _REQUEST for data, _ in transport.sent)

    async def test_a_down_interface_does_not_stop_the_others(self) -> None:
        """This machine has several adapters; one failing is normal."""
        transport = FakeTransport()
        transport.unreachable = {"192.0.2.255"}

        await async_discover(
            ["192.0.2.255", "198.51.100.255"],
            timeout=0,
            attempts=1,
            endpoint_factory=transport.factory(),
        )

        assert [addr[0] for _, addr in transport.sent] == ["198.51.100.255"]

    async def test_a_device_that_answers_is_returned(self) -> None:
        transport = FakeTransport({"192.0.2.10": _reply()})

        found = await async_discover(
            ["192.0.2.255"],
            timeout=0,
            attempts=1,
            endpoint_factory=transport.factory(),
        )

        assert [spa.address for spa in found] == ["192.0.2.10"]
        assert found[0].product_key == "747be354e00449e799883a966d0c9cbd"

    async def test_several_devices_are_all_returned(self) -> None:
        """Stopping at the first would find one spa and miss the rest."""
        transport = FakeTransport(
            {
                "192.0.2.10": _reply(did=b"A" * 22),
                "192.0.2.11": _reply(did=b"B" * 22),
            }
        )

        found = await async_discover(
            ["192.0.2.255"],
            timeout=0,
            attempts=1,
            endpoint_factory=transport.factory(),
        )

        assert sorted(spa.did for spa in found) == ["A" * 22, "B" * 22]

    async def test_something_that_is_not_a_spa_is_dropped(self) -> None:
        """Any Gizwits product answers this broadcast, not just spas."""
        transport = FakeTransport(
            {"192.0.2.10": _reply(), "192.0.2.11": b"some other protocol"}
        )

        found = await async_discover(
            ["192.0.2.255"],
            timeout=0,
            attempts=1,
            endpoint_factory=transport.factory(),
        )

        assert [spa.address for spa in found] == ["192.0.2.10"]

    async def test_the_socket_is_always_closed(self) -> None:
        transport = FakeTransport()

        await async_discover(
            ["192.0.2.255"],
            timeout=0,
            attempts=1,
            endpoint_factory=transport.factory(),
        )

        assert transport.closed is True
