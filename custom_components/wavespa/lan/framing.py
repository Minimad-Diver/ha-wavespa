"""The Gizwits LAN transport envelope.

Every packet, in both directions, looks like this:

    00 00 00 03 | varlen | flag | cmd (2 bytes) | payload

The version prefix is 3. The product definition declares
``"packetVersion": "0x00000004"``, which describes the *datapoint payload*
format rather than this envelope - sending discovery with a version 4 prefix
gets no reply at all, tested repeatedly against a real spa, while version 3
answers every time.

Nothing here is product-specific.
"""

from __future__ import annotations

from dataclasses import dataclass

PROTOCOL_VERSION = b"\x00\x00\x00\x03"

# Commands, named from the device's point of view where "response" means the
# device answering us.
CMD_DISCOVER = 0x0003
CMD_DISCOVER_RESPONSE = 0x0004
CMD_PASSCODE = 0x0006
CMD_PASSCODE_RESPONSE = 0x0007
CMD_LOGIN = 0x0008
CMD_LOGIN_RESPONSE = 0x0009
CMD_PING = 0x0015
CMD_PONG = 0x0016
CMD_STATUS = 0x0090
CMD_STATUS_RESPONSE = 0x0091
CMD_WRITE = 0x0093
CMD_WRITE_ACK = 0x0094


class FramingError(ValueError):
    """A packet could not be parsed."""


@dataclass(frozen=True)
class Frame:
    """One decoded packet."""

    cmd: int
    payload: bytes = b""
    # Normally 0. The device sets it to 1 on updates we did not ask for, which
    # is how an unsolicited status push is told apart from an answer.
    flag: int = 0

    @property
    def is_unsolicited(self) -> bool:
        """Whether the device sent this of its own accord."""
        return self.flag == 1


def encode_varlen(value: int) -> bytes:
    """Encode a length the way Gizwits does.

    Under 128 is a single byte. Above that it is 7 bits per byte, little end
    first, with the top bit set on every byte except the last.
    """
    if value < 0:
        raise ValueError("length cannot be negative")
    if value < 0x80:
        return bytes([value])

    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_varlen(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a length, returning it with the offset just past it."""
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 28:
            raise FramingError("length field is implausibly long")
    raise FramingError("packet ended inside its length field")


def pack(cmd: int, payload: bytes = b"", flag: int = 0) -> bytes:
    """Build a packet ready to send."""
    if not 0 <= cmd <= 0xFFFF:
        raise ValueError(f"command {cmd} does not fit in two bytes")

    body = bytes([flag]) + cmd.to_bytes(2, "big") + payload
    return PROTOCOL_VERSION + encode_varlen(len(body)) + body


def unpack(data: bytes) -> Frame:
    """Parse one packet.

    Raises FramingError rather than returning None, because a packet we cannot
    read is a protocol problem worth surfacing - silently ignoring it would
    make a device that has changed behaviour look merely quiet.
    """
    if not data.startswith(PROTOCOL_VERSION):
        raise FramingError(
            f"expected prefix {PROTOCOL_VERSION.hex()}, got {data[:4].hex()}"
        )

    body_len, offset = decode_varlen(data, len(PROTOCOL_VERSION))
    body = data[offset:]

    if len(body) < body_len:
        raise FramingError(
            f"packet claims {body_len} bytes of body but carries {len(body)}"
        )
    # A longer body than declared means more than one packet arrived in the
    # same read; take only what this one claims.
    body = body[:body_len]

    if len(body) < 3:
        raise FramingError("body is too short to hold a flag and a command")

    return Frame(
        flag=body[0],
        cmd=int.from_bytes(body[1:3], "big"),
        payload=body[3:],
    )


def split_stream(buffer: bytes) -> tuple[list[Frame], bytes]:
    """Pull whole packets out of a TCP buffer, returning them and the remainder.

    TCP gives no message boundaries, so a read can hold several packets, or
    half of one. Anything incomplete is handed back to be prepended to the next
    read rather than discarded.
    """
    frames: list[Frame] = []

    while True:
        if not buffer.startswith(PROTOCOL_VERSION):
            break
        try:
            body_len, offset = decode_varlen(buffer, len(PROTOCOL_VERSION))
        except FramingError:
            break  # length field itself is still arriving
        if len(buffer) < offset + body_len:
            break  # body still arriving

        frames.append(unpack(buffer[: offset + body_len]))
        buffer = buffer[offset + body_len :]

    return frames, buffer
