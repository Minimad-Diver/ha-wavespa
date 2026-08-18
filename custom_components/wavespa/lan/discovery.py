"""Find Gizwits devices on the local network.

One UDP broadcast, and every device on the subnet answers with its identity.
That is enough to turn "which address is my spa on?" from something the user
has to look up in their router into something the integration can work out.

Read-only, and cheap: discovery does not touch the TCP control port, so it
costs none of the device's small pool of connection slots. It can be repeated
freely, unlike opening a session.

Deliberately knows nothing about Home Assistant - broadcast addresses are
passed in rather than discovered here. The integration layer gets them from
`homeassistant.components.network`; tests pass whatever they like, and this
module stays testable without a network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from logging import getLogger

from .framing import CMD_DISCOVER, CMD_DISCOVER_RESPONSE, FramingError, pack, unpack

_LOGGER = getLogger(__name__)

# Devices listen for the broadcast here, and answer from an ephemeral port.
DISCOVERY_PORT = 12414

# How long to wait for replies after each broadcast. Five seconds because a
# real spa takes a few seconds to answer - a two-second window finds nothing
# at all, which looks exactly like there being no spa.
DEFAULT_TIMEOUT = 5.0

# Broadcasts are unreliable by nature, so ask more than once. A device that
# missed the first request usually answers the second.
DEFAULT_ATTEMPTS = 2

_REQUEST = pack(CMD_DISCOVER)

# Opens the UDP socket. Injectable so a sweep can be tested without a network,
# the same way GizwitsLanSession takes a connector.
EndpointFactory = Callable[
    [asyncio.DatagramProtocol], Awaitable[tuple[asyncio.DatagramTransport, None]]
]


@dataclass(frozen=True)
class DiscoveredSpa:
    """One device that answered the broadcast."""

    address: str
    # The unit's own identifier. Expected to match the `did` the bindings API
    # returns, which is what makes matching a discovered device to a
    # configured one possible - verify that before relying on it.
    did: str
    # Six bytes, lower-case hex, no separators. Formatting is left to the
    # caller so this module needs nothing from Home Assistant.
    mac: str
    hardware_id: str
    # Identifies the model, not the unit, and is the lookup key for the
    # datapoint layout its status is decoded with.
    product_key: str


def _leading_fields(payload: bytes, count: int) -> list[bytes] | None:
    """Read the first `count` length-prefixed fields, or None if malformed.

    Each field is a two-byte big-endian length followed by that many bytes.
    Only the leading fields are read: what follows them is a mixture of
    padding and free-form strings that does not reliably continue the same
    pattern, and nothing here needs it.
    """
    fields: list[bytes] = []
    offset = 0

    for _ in range(count):
        if offset + 2 > len(payload):
            return None
        length = int.from_bytes(payload[offset : offset + 2], "big")
        offset += 2
        if length == 0 or offset + length > len(payload):
            return None
        fields.append(payload[offset : offset + length])
        offset += length

    return fields


def parse_discovery_reply(address: str, data: bytes) -> DiscoveredSpa | None:
    """Turn one reply into a DiscoveredSpa, or None if it is not one.

    Tolerant on purpose. Any Gizwits product on the network answers this
    broadcast, not just spas, and a reply we cannot read is something to skip
    rather than an error - the caller decides which product it cares about.
    """
    try:
        frame = unpack(data)
    except FramingError as err:
        _LOGGER.debug("Ignoring an unparsable reply from %s: %s", address, err)
        return None

    if frame.cmd != CMD_DISCOVER_RESPONSE:
        _LOGGER.debug(
            "Ignoring command 0x%04x from %s; expected a discovery reply",
            frame.cmd,
            address,
        )
        return None

    fields = _leading_fields(frame.payload, 4)
    if fields is None:
        _LOGGER.debug("Reply from %s did not carry the expected fields", address)
        return None

    did, mac, hardware_id, product_key = fields

    try:
        return DiscoveredSpa(
            address=address,
            did=did.decode("ascii"),
            mac=mac.hex(),
            hardware_id=hardware_id.decode("ascii"),
            product_key=product_key.decode("ascii"),
        )
    except UnicodeDecodeError:
        _LOGGER.debug("Reply from %s held non-ascii identifiers", address)
        return None


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Collects one reply per address."""

    def __init__(self) -> None:
        self.replies: dict[str, bytes] = {}

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        address = str(addr[0])
        if data == _REQUEST:
            # Our own broadcast, looped back by the host.
            return
        # First reply wins: a device that answers both attempts should not be
        # reported twice, and the answers are identical anyway.
        self.replies.setdefault(address, data)

    def error_received(self, exc: Exception) -> None:
        # An ICMP error from one address must not abandon the whole sweep.
        _LOGGER.debug("Discovery socket reported: %s", exc)


async def async_discover(
    broadcast_addresses: Iterable[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    endpoint_factory: EndpointFactory | None = None,
) -> list[DiscoveredSpa]:
    """Broadcast for devices and return whatever answers.

    Every attempt is made even after something replies, because a second spa
    may simply be slower than the first - stopping early would find one device
    on a multi-spa network and quietly miss the rest.

    An empty list is not proof of absence. Broadcast traffic is swallowed by
    AP isolation and by many guest networks, and a host firewall blocking the
    reply looks identical from here.
    """
    targets = list(broadcast_addresses)
    if not targets:
        _LOGGER.debug("No broadcast addresses to try")
        return []

    protocol = _DiscoveryProtocol()
    if endpoint_factory is None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            local_addr=("0.0.0.0", 0),
            allow_broadcast=True,
        )
    else:
        transport, _ = await endpoint_factory(protocol)

    try:
        for attempt in range(attempts):
            for target in targets:
                try:
                    transport.sendto(_REQUEST, (target, DISCOVERY_PORT))
                except OSError as err:
                    # A down interface should not stop the others being tried.
                    _LOGGER.debug("Broadcast to %s failed: %s", target, err)
            _LOGGER.debug(
                "Discovery attempt %d of %d sent, listening %.0fs",
                attempt + 1,
                attempts,
                timeout,
            )
            await asyncio.sleep(timeout)
    finally:
        transport.close()

    found = [
        spa
        for address, data in sorted(protocol.replies.items())
        if (spa := parse_discovery_reply(address, data)) is not None
    ]
    _LOGGER.debug("Discovery finished: %d device(s) answered", len(found))
    return found
