"""Turn a Gizwits datapoint payload into attributes, and back.

The layout is not hardcoded. It comes from the product's own definition,
fetched at runtime from ``{api_root}/app/datapoint?product_key=...``, so a new
spa model needs no code change - only its product key, which the bindings
response already carries.

The names in that definition are the same ones the cloud API returns, so a
decoded payload is directly comparable with `WavespaDeviceStatus.attrs`. That
is what lets the LAN become another source of the same state rather than a
parallel one.

Verified against a real Wave Spa Garda: its reported state round-trips to
``05 18 00 00 00 1a 00 16 00`` and back unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from logging import getLogger
from typing import Any

_LOGGER = getLogger(__name__)

# Types seen in Wave Spa's definition. Anything else is skipped rather than
# guessed at, because a wrong offset on a writable datapoint means writing to
# the wrong field on real hardware.
_SUPPORTED_TYPES = frozenset({"bool", "uint8", "uint16", "uint32"})

_BYTE_WIDTH = {"bool": 1, "uint8": 1, "uint16": 2, "uint32": 4}

# The datapoints this integration's entities read by name. A definition without
# them decodes perfectly well, but every entity built on it would sit at
# unknown forever, so a LAN session is refused rather than half-working.
#
# The codec itself stays product-agnostic; this is the integration's
# requirement of a product, not the protocol's. The alert datapoints are
# deliberately absent - the Alerts sensor tolerates their absence, so a product
# without them is still worth talking to.
REQUIRED_DATAPOINTS = frozenset(
    {
        "Bubble",
        "Current_temperature",
        "Filter",
        "Heater",
        "Temperature_setup",
        "Time_filter",
    }
)


class CodecError(ValueError):
    """A payload or definition could not be interpreted."""


@dataclass(frozen=True)
class Datapoint:
    """One field within the payload."""

    name: str
    data_type: str
    byte_offset: int
    bit_offset: int
    # Bit count for bools, byte count otherwise - the definition overloads
    # `len` according to `position.unit`.
    length: int
    writable: bool
    is_alert: bool

    @property
    def width(self) -> int:
        """How many bytes this datapoint spans."""
        if self.data_type == "bool":
            return 1
        return _BYTE_WIDTH.get(self.data_type, self.length)


class DatapointSchema:
    """A product's datapoint definition, ready to encode and decode with."""

    def __init__(self, definition: dict[str, Any]) -> None:
        """Build a schema from a definition as the API returns it."""
        self.product_key: str = definition.get("product_key", "")
        self.name: str = definition.get("name", "")
        self.datapoints: list[Datapoint] = []

        for entity in definition.get("entities", []):
            for attr in entity.get("attrs", []):
                datapoint = self._parse_attr(attr)
                if datapoint is not None:
                    self.datapoints.append(datapoint)

        if not self.datapoints:
            raise CodecError("definition contains no usable datapoints")

    @staticmethod
    def _parse_attr(attr: dict[str, Any]) -> Datapoint | None:
        """Convert one attr entry, or None if we cannot handle it safely.

        Every skip is logged. Dropping a datapoint costs an entity its value,
        and without a line in the log the only symptom is a sensor that never
        populates - with nothing to connect it to the product definition.
        """
        data_type = attr.get("data_type")
        name = attr.get("name")
        position = attr.get("position") or {}

        if not name:
            _LOGGER.warning(
                "Product definition contains a datapoint with no name; skipping it"
            )
            return None
        if data_type not in _SUPPORTED_TYPES:
            _LOGGER.warning(
                "Skipping datapoint '%s': the codec does not handle data type '%s', "
                "so anything reading it will have no value over the LAN",
                name,
                data_type,
            )
            return None
        if "byte_offset" not in position:
            _LOGGER.warning(
                "Skipping datapoint '%s': the product definition gives it no "
                "byte offset, and guessing one risks reading the wrong field",
                name,
            )
            return None

        return Datapoint(
            name=name,
            data_type=data_type,
            byte_offset=int(position["byte_offset"]),
            bit_offset=int(position.get("bit_offset", 0)),
            length=int(position.get("len", 1)),
            # "status_writable" is writable; "status_readonly" and "alert"
            # are not.
            writable=attr.get("type") == "status_writable",
            is_alert=attr.get("type") == "alert",
        )

    @property
    def payload_size(self) -> int:
        """The number of bytes a full status payload occupies."""
        return max(dp.byte_offset + dp.width for dp in self.datapoints)

    def by_name(self, name: str) -> Datapoint | None:
        """Look up a datapoint, or None if the product has no such field."""
        return next((dp for dp in self.datapoints if dp.name == name), None)

    @property
    def alert_names(self) -> tuple[str, ...]:
        """Datapoints the manufacturer types as faults."""
        return tuple(dp.name for dp in self.datapoints if dp.is_alert)

    @property
    def writable_names(self) -> tuple[str, ...]:
        """Datapoints that may be written."""
        return tuple(dp.name for dp in self.datapoints if dp.writable)

    def missing(self, names: frozenset[str]) -> tuple[str, ...]:
        """Return which of `names` this product does not provide, sorted."""
        return tuple(sorted(names - {dp.name for dp in self.datapoints}))


def require_datapoints(
    schema: DatapointSchema, names: frozenset[str] = REQUIRED_DATAPOINTS
) -> None:
    """Raise unless the product provides every datapoint the entities need.

    Called before a LAN session is opened. A definition can be entirely valid
    and still be useless to us - a different product, or a renamed field - and
    the failure mode without this check is silent: the codec decodes happily
    into names nothing looks up, so every entity reports unknown with nothing
    in the log to say why. Better to refuse the LAN and stay on the cloud.
    """
    missing = schema.missing(names)
    if missing:
        raise CodecError(
            f"product '{schema.name or schema.product_key}' has no "
            f"{', '.join(missing)} datapoint(s); its definition cannot drive "
            "this integration's entities"
        )


def decode_attrs(schema: DatapointSchema, payload: bytes) -> dict[str, Any]:
    """Read a status payload into the attribute dictionary the cloud returns.

    A payload shorter than the schema expects is an error rather than a
    partial read: guessing which fields are present would put wrong values in
    front of the user, and silently dropping the tail is worse than saying so.
    """
    if len(payload) < schema.payload_size:
        raise CodecError(
            f"payload is {len(payload)} bytes, schema needs {schema.payload_size}"
        )

    attrs: dict[str, Any] = {}
    for dp in schema.datapoints:
        if dp.data_type == "bool":
            attrs[dp.name] = (payload[dp.byte_offset] >> dp.bit_offset) & 1
        else:
            raw = payload[dp.byte_offset : dp.byte_offset + dp.width]
            attrs[dp.name] = int.from_bytes(raw, "big")

    return attrs


def encode_attrs(schema: DatapointSchema, attrs: dict[str, Any]) -> bytes:
    """Build a full status payload from attributes.

    Mainly for tests and for round-tripping: a write uses the partial form,
    which carries only the fields being changed.

    A name the product does not have is an error. Skipping it would encode a
    payload that quietly omits what the caller asked for, and once this feeds
    the write path that is a command reported as sent which the spa never
    acted on. Callers holding a cloud attribute dictionary that may carry
    extra fields should filter it against `schema.by_name` first, so that
    dropping a field is something they decided rather than something that
    happened to them.
    """
    buffer = bytearray(schema.payload_size)

    for name, value in attrs.items():
        dp = schema.by_name(name)
        if dp is None:
            raise CodecError(f"product has no datapoint named '{name}'")

        if dp.data_type == "bool":
            if int(value):
                buffer[dp.byte_offset] |= 1 << dp.bit_offset
        else:
            buffer[dp.byte_offset : dp.byte_offset + dp.width] = int(value).to_bytes(
                dp.width, "big"
            )

    return bytes(buffer)
