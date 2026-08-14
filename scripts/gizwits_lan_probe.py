#!/usr/bin/env python3
"""Probe the local network for Gizwits devices (read-only).

A research tool, not part of the integration - nothing imports it and it
ships no behaviour. It exists to answer whether local (LAN) control is
possible for a spa, which the cloud API cannot tell us.

Answers three questions before any code is written against the LAN protocol:

  1. Does the spa speak the Gizwits LAN protocol at all? It may be disabled in
     firmware, in which case the whole idea stops here.
  2. What is its product_key? That is the lookup key for the datapoint
     definition, without which a payload is undecodable bytes.
  3. Does it accept an unauthenticated TCP connection on 12416?

Sends one UDP broadcast and, if anything answers, opens a TCP connection and
closes it immediately. Nothing is written to any device.

Usage:
    python gizwits_lan_probe.py [--timeout 5] [--retries 3] [--no-tcp]

Windows may raise a firewall prompt the first time, because the script listens
for UDP replies. Allow it on private networks or the replies are dropped
silently and this reports nothing found.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import socket
import sys
from dataclasses import dataclass, field

# Gizwits LAN protocol. Ports and the discovery payload are the same across
# products - only the datapoint encoding is device-specific.
DISCOVERY_PORT = 12414
CONTROL_PORT = 12416
DISCOVERY_REQUEST = b"\x00\x00\x00\x03\x03\x00\x00\x03"

# The transport envelope is version 3. Do not be tempted by the product
# definition's "packetVersion": "0x00000004" - that describes the datapoint
# payload format, a different layer. Tested against a real spa with the
# prefix swapped to \x00\x00\x00\x04: no reply at all, across repeated runs
# in both orders, while version 3 answered every time.
PROTOCOL_PREFIX = b"\x00\x00\x00\x03"

# A Gizwits product key is 32 lowercase hex characters. Distinctive enough to
# pick out of a payload without knowing the field layout.
PRODUCT_KEY_RE = re.compile(rb"[0-9a-f]{32}")
PRINTABLE_RUN_RE = re.compile(rb"[ -~]{6,}")


@dataclass
class Reply:
    """One response to the discovery broadcast."""

    address: str
    raw: bytes
    cmd: int | None = None
    payload: bytes = b""
    strings: list[str] = field(default_factory=list)
    product_keys: list[str] = field(default_factory=list)


def _read_varlen(data: bytes, offset: int) -> tuple[int, int]:
    """Read Gizwits' variable-length integer, returning (value, new_offset).

    Values under 128 are a single byte; larger ones are 7-bit chunks with the
    top bit set on every byte but the last.
    """
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("truncated length field")


def parse_reply(address: str, raw: bytes) -> Reply:
    """Pull what we can out of a reply without knowing the payload layout.

    Deliberately tolerant: the goal is to learn whether a device answers and
    what its product key is, not to implement the protocol.
    """
    reply = Reply(address=address, raw=raw)

    if raw.startswith(PROTOCOL_PREFIX):
        try:
            _body_len, offset = _read_varlen(raw, len(PROTOCOL_PREFIX))
            # flag byte, then a two-byte command
            offset += 1
            reply.cmd = int.from_bytes(raw[offset : offset + 2], "big")
            reply.payload = raw[offset + 2 :]
        except (ValueError, IndexError):
            reply.payload = raw[len(PROTOCOL_PREFIX) :]
    else:
        reply.payload = raw

    reply.strings = [
        match.group().decode("ascii", "replace")
        for match in PRINTABLE_RUN_RE.finditer(raw)
    ]
    reply.product_keys = [
        match.group().decode("ascii") for match in PRODUCT_KEY_RE.finditer(raw)
    ]
    return reply


def broadcast_addresses() -> list[str]:
    """Every broadcast address worth trying.

    A machine with several adapters - VPN, Docker, a NAS link - will happily
    send the global broadcast out of the wrong one, so each interface's own
    /24 broadcast is tried as well.
    """
    addresses = ["255.255.255.255"]

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            # sockaddr is a union across address families, so the first element
            # is only a string for the IPv4 case we asked for.
            local_ip = info[4][0]
            if not isinstance(local_ip, str) or local_ip.startswith("127."):
                continue
            network = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
            candidate = str(network.broadcast_address)
            if candidate not in addresses:
                addresses.append(candidate)
    except OSError as err:  # pragma: no cover - host-dependent
        print(f"  (could not enumerate local interfaces: {err})")

    return addresses


def discover(timeout: float, retries: int) -> dict[str, Reply]:
    """Broadcast the discovery request and collect replies."""
    replies: dict[str, Reply] = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)

    try:
        sock.bind(("0.0.0.0", 0))
        targets = broadcast_addresses()
        print(f"Broadcasting on UDP {DISCOVERY_PORT} to: {', '.join(targets)}")
        print(f"Listening for {timeout:.0f}s per attempt, {retries} attempts.\n")

        for attempt in range(1, retries + 1):
            for target in targets:
                try:
                    sock.sendto(DISCOVERY_REQUEST, (target, DISCOVERY_PORT))
                except OSError as err:
                    print(f"  send to {target} failed: {err}")

            while True:
                try:
                    raw, (address, _port) = sock.recvfrom(2048)
                except socket.timeout:
                    break
                except OSError as err:
                    print(f"  receive error: {err}")
                    break

                if raw == DISCOVERY_REQUEST:
                    continue  # our own broadcast looping back
                if address not in replies:
                    print(f"  attempt {attempt}: reply from {address}")
                    replies[address] = parse_reply(address, raw)
    finally:
        sock.close()

    return replies


def check_control_port(address: str, timeout: float) -> str:
    """Open and immediately close a TCP connection. Nothing is sent."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((address, CONTROL_PORT))
        return "open (accepts connections without authentication)"
    except socket.timeout:
        return "timed out (filtered?)"
    except ConnectionRefusedError:
        return "refused (nothing listening)"
    except OSError as err:
        return f"error: {err}"
    finally:
        sock.close()


def report(replies: dict[str, Reply], check_tcp: bool, timeout: float) -> int:
    """Print the findings. Returns a process exit code."""
    if not replies:
        print("No Gizwits devices answered.\n")
        print("That is a real result, but rule out the boring causes first:")
        print("  - the spa is powered on and on the same subnet as this machine")
        print("  - Windows Firewall allowed this script to receive UDP")
        print("  - the network does not block broadcast traffic between clients")
        print("    (guest networks and some mesh systems do, and AP isolation")
        print("     will silently swallow the request)")
        print("\nIf all of those are fine, the firmware likely has the LAN")
        print("protocol disabled, and local control is not available.")
        return 1

    print(f"\n{'=' * 68}")
    print(f"{len(replies)} device(s) answered")
    print("=" * 68)

    for address, reply in sorted(replies.items()):
        print(f"\n{address}")
        print(f"  bytes    : {len(reply.raw)}")
        if reply.cmd is not None:
            print(f"  command  : 0x{reply.cmd:04x}")
        print(f"  raw hex  : {reply.raw.hex()}")

        if reply.product_keys:
            for key in reply.product_keys:
                print(f"  PRODUCT KEY: {key}   <-- this is what we need")
        else:
            print("  product key: not found in the reply")

        if reply.strings:
            print("  strings  :")
            for text in reply.strings:
                print(f"    {text!r}")

        if check_tcp:
            print(f"  tcp {CONTROL_PORT}: {check_control_port(address, timeout)}")

    print(f"\n{'=' * 68}")
    found_keys = {key for reply in replies.values() for key in reply.product_keys}
    if found_keys:
        print("Next step: the datapoint definition for", ", ".join(sorted(found_keys)))
        print("is what turns these bytes into named attributes. Without it a")
        print("payload is undecodable, so that is the thing to go and find.")
    else:
        print("Something answered but no product key was visible. The raw hex")
        print("above is still worth keeping - the key may be encoded rather")
        print("than sent as ASCII.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--no-tcp",
        action="store_true",
        help="skip the TCP connectivity check",
    )
    args = parser.parse_args()

    print("Gizwits LAN probe - read-only, nothing is written to any device\n")
    replies = discover(timeout=args.timeout, retries=args.retries)
    return report(replies, check_tcp=not args.no_tcp, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
