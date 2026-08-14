"""Gizwits LAN protocol support.

Groundwork for controlling the spa directly over the local network instead of
through the Gizwits cloud. Nothing in the integration imports this yet - it is
built and tested in isolation first, so that a released component cannot be
destabilised by work in progress.

The protocol splits cleanly into two layers, and this package currently covers
only the two that need no device to test:

- `framing`: the transport envelope, identical for every Gizwits product.
- `codec`: turning a datapoint payload into the same attribute dictionary the
  cloud API returns, driven by the product's own definition.

The network session on top of them lands separately.

Protocol details were derived from chrisc123/jebao_aqua-homeassistant (MIT),
and verified against a real Wave Spa Garda.
"""

from .codec import DatapointSchema, decode_attrs, encode_attrs
from .framing import Frame, pack, unpack

__all__ = [
    "DatapointSchema",
    "Frame",
    "decode_attrs",
    "encode_attrs",
    "pack",
    "unpack",
]
