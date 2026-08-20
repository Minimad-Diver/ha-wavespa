"""Gizwits LAN protocol support.

Reads the spa's state directly over the local network instead of through the
Gizwits cloud. Used by the coordinator when a spa address is configured in the
integration options; with none set, local control is off and the cloud is the
only transport.

The session can write datapoints as well as read them. Whether the
integration's entities route their commands here or through the cloud is a
separate decision, made above this package.

- `framing`: the transport envelope, identical for every Gizwits product.
- `codec`: turning a datapoint payload into the same attribute dictionary the
  cloud API returns, driven by the product's own definition.
- `session`: a TCP session - connect, authenticate, read status, and stay
  alive, reporting state changes the spa announces unprompted.

Protocol details were derived from chrisc123/jebao_aqua-homeassistant (MIT),
and verified against a real Wave Spa Garda.
"""

from .codec import (
    REQUIRED_DATAPOINTS,
    CodecError,
    DatapointSchema,
    decode_attrs,
    encode_attrs,
    encode_write,
    require_datapoints,
)
from .framing import Frame, pack, unpack
from .session import GizwitsLanSession, LanSessionError, LoginRefused

__all__ = [
    "REQUIRED_DATAPOINTS",
    "CodecError",
    "DatapointSchema",
    "Frame",
    "GizwitsLanSession",
    "LanSessionError",
    "LoginRefused",
    "decode_attrs",
    "encode_attrs",
    "encode_write",
    "pack",
    "require_datapoints",
    "unpack",
]
