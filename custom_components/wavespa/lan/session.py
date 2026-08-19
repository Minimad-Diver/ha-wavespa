"""A TCP session with one spa over the local network.

Read-only. Connect, authenticate, read status, stay alive. Writing datapoints
is deliberately absent: a wrong byte offset on a write changes physical state
on someone's spa, so it belongs in its own change with its own review.

The shape follows GizwitsWebSocket - a supervisor loop around a single
connection attempt, an asyncio.Event for shutdown, exponential backoff - both
because that pattern is already proven in this integration and because the two
transports have the same job.

One rule drove the design, learned by getting it wrong twice against real
hardware: **the keepalive must run concurrently with any request**. A session
that sends a status request and then blocks waiting for the reply, without
pinging meanwhile, gets no answer at all - and after roughly eight seconds of
silence the device aborts the connection. That failure looks exactly like a
wrong command code, which is a good way to waste an afternoon. So pings run on
their own task and requests are correlated against whatever the receive loop
hands back.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from .codec import CodecError, DatapointSchema, decode_attrs, require_datapoints
from .framing import (
    CMD_LOGIN,
    CMD_LOGIN_RESPONSE,
    CMD_MODULE_INFO,
    CMD_MODULE_INFO_RESPONSE,
    CMD_PASSCODE,
    CMD_PASSCODE_RESPONSE,
    CMD_PING,
    CMD_STATUS,
    CMD_STATUS_RESPONSE,
    P0_READ,
    Frame,
    FramingError,
    pack,
    split_stream,
)

_LOGGER = getLogger(__name__)

DEFAULT_PORT = 12416

# Ping cadence. Three seconds is what has been verified against real hardware;
# an eight-second gap is known to get the connection aborted. The device is on
# the LAN and the packets are empty, so the cost of the margin is nothing.
_PING_INTERVAL = 3.0

# How long to wait for a reply to a request. Generous because the ping task
# keeps the session alive throughout, so a slow answer costs nothing.
_REPLY_TIMEOUT = 10.0

_CONNECT_TIMEOUT = 10.0

# Reconnection backoff, matching the WebSocket client's schedule.
_RECONNECT_DELAYS = (3, 6, 12, 24, 48, 60)

# Connector signature, injectable so the session can be tested without a spa.
Connector = Callable[
    [str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
]

# Looks up where the spa is now, returning an address or None if it cannot
# say. Called only after repeated connection failures, so a spa whose DHCP
# lease moved is found again instead of the session retrying a dead address
# forever. Kept as a plain callable so this module needs to know nothing about
# discovery, and nothing about Home Assistant.
AddressResolver = Callable[[], Awaitable[str | None]]

# How many failed attempts before asking where the spa went. Not the first
# failure: a spa is briefly unreachable for all sorts of ordinary reasons -
# a reboot, a wifi blip - and re-resolving on each of those would be noise.
_REDISCOVER_AFTER_FAILURES = 3


class LanSessionError(Exception):
    """The session could not be established or was lost."""


class LoginRefused(LanSessionError):
    """The device rejected the passcode."""


async def _open_connection(
    host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(host, port)


@dataclass(frozen=True)
class ModuleInfo:
    """What the wifi module reports about itself, from a 0x0014 reply.

    Only the fields that could be named with confidence. The reply also
    carries a 24-character mostly-zero field whose meaning is unknown, and it
    notably does NOT carry the GAgent version - that appears in the discovery
    broadcast reply instead.

    Worth having because discovery cannot run at all when Home Assistant is
    containerised with bridge networking, so for those installs this is the
    only way to learn the hardware id.
    """

    module: str
    hardware_id: str
    product_key: str


def parse_module_info(payload: bytes) -> ModuleInfo | None:
    """Read a 0x0014 payload, or None if it is not the shape we know.

    Observed against a real Wave Spa Garda, 84 bytes:

        0-1    "00"
        2-7    module family, e.g. "ESP826"
        8-15   hardware id, identical to the one discovery reports
        16-39  24 characters, mostly zeros, meaning unknown
        40-49  padding
        50-51  a two-byte length, 32
        52-83  the product key

    The offsets are not guesswork: the hardware id and product key parsed out
    of them match the values discovery and the cloud report independently.
    """
    if len(payload) < 52:
        _LOGGER.debug("Module info payload is %d bytes, too short", len(payload))
        return None

    length = int.from_bytes(payload[50:52], "big")
    key = payload[52 : 52 + length]
    if len(key) != length:
        _LOGGER.debug("Module info payload claims a %d-byte key it lacks", length)
        return None

    try:
        return ModuleInfo(
            module=payload[2:8].decode("ascii").strip("\x00"),
            hardware_id=payload[8:16].decode("ascii").strip("\x00"),
            product_key=key.decode("ascii"),
        )
    except UnicodeDecodeError:
        _LOGGER.debug("Module info payload held non-ascii identifiers")
        return None


class GizwitsLanSession:
    """Keeps one spa's state current over the LAN."""

    def __init__(
        self,
        host: str,
        schema: DatapointSchema,
        update_callback: Callable[[dict[str, Any]], None],
        *,
        port: int = DEFAULT_PORT,
        connect_callback: Callable[[], None] | None = None,
        disconnect_callback: Callable[[], None] | None = None,
        connector: Connector | None = None,
        address_resolver: AddressResolver | None = None,
    ) -> None:
        """Initialize the session.

        Args:
            host: the spa's address on the LAN
            schema: the product's datapoint definition, used to decode status
            update_callback: called with decoded attributes on every status,
                solicited or pushed
            port: control port, 12416 unless a device says otherwise
            connect_callback: called once the session is usable
            disconnect_callback: called when an established session drops
            connector: opens the connection; injectable for tests
            address_resolver: asked where the spa is after repeated failures,
                so a changed address is picked up rather than retried forever

        Raises:
            CodecError: the schema lacks datapoints the entities need, so a
                session against it could never produce usable state.
        """
        require_datapoints(schema)

        self._host = host
        self._port = port
        self._schema = schema
        self._update_callback = update_callback
        self._connect_callback = connect_callback
        self._disconnect_callback = disconnect_callback
        self._connector: Connector = connector or _open_connection
        self._address_resolver = address_resolver

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._close_event = asyncio.Event()
        self._reconnect_count = 0
        self._connected = False

        # Filled on the first successful connection. The module cannot
        # change what it reports, so it is asked once rather than per
        # connection.
        self.module_info: ModuleInfo | None = None

        # Requests awaiting a reply, keyed by the command they expect back.
        self._waiters: dict[int, asyncio.Future[Frame]] = {}

    @property
    def is_connected(self) -> bool:
        """Whether the session is authenticated and usable."""
        return self._connected

    async def async_run(self) -> None:
        """Keep a session alive until disconnect() is called.

        Meant to be awaited as a long-lived background task. Each pass makes
        one connection attempt and then serves it until the connection drops;
        retries happen in the loop rather than by re-entering connect(), so the
        call stack stays flat however long an outage lasts.
        """
        self._close_event.clear()

        while not self._close_event.is_set():
            was_connected = False
            try:
                await self.connect()
                was_connected = True
                self._reconnect_count = 0
                self._notify(self._connect_callback, "connect")
                await self._serve()
            except asyncio.CancelledError:
                raise
            except CodecError as err:
                # The spa's payload does not match the schema. Retrying cannot
                # fix that, and the device has a small fixed pool of connection
                # slots - reconnecting forever against a spa we can never
                # decode leaves it answering nothing at all for about five
                # minutes, the phone app included. Stop, and leave the cloud
                # transport to it.
                _LOGGER.error(
                    "LAN session to %s cannot decode this spa's status: %s. "
                    "Giving up on the LAN; the integration carries on over "
                    "the cloud",
                    self._host,
                    err,
                )
                self._close_event.set()
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("LAN session to %s failed: %s", self._host, err)
            finally:
                await self._teardown()

            # A permanent decode failure sets the close event above, so it
            # leaves here the same way disconnect() does: an orderly stop, not
            # a dropped connection, and so not a disconnect callback.
            if self._close_event.is_set():
                break
            if was_connected:
                self._notify(self._disconnect_callback, "disconnect")

            await self._maybe_rediscover()
            await self._wait_before_retry()

        _LOGGER.debug("LAN supervisor for %s stopped", self._host)

    async def connect(self) -> None:
        """Make one connection attempt, authenticate, and read initial state.

        The passcode and login exchanges run sequentially, reading the socket
        inline - that is what has been verified against hardware, and it
        completes in well under the device's timeout. Everything after them
        goes through the receive and ping tasks, which start together: from
        that point on no reply is ever awaited with pings paused.

        Once this returns the session is fully usable on its own. Callers that
        want it kept alive across outages should use async_run() instead.
        """
        if self._connected:
            _LOGGER.debug("LAN session to %s already connected", self._host)
            return

        async with asyncio.timeout(_CONNECT_TIMEOUT):
            self._reader, self._writer = await self._connector(self._host, self._port)

        _LOGGER.debug("Connected to %s:%s, requesting passcode", self._host, self._port)

        try:
            passcode = await self._fetch_passcode()
            await self._login(passcode)
        except Exception:
            # A half-open socket would leak; the supervisor also tears down,
            # but connect() is public and _teardown() is idempotent.
            await self._teardown()
            raise

        self._connected = True
        self._receive_task = asyncio.create_task(self._receive_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

        # Prime the cache so entities have state without waiting for a push.
        await self.request_status()

        # Best-effort and once only. It is useful for bug reports but nothing
        # depends on it, so a module that will not answer must not cost us the
        # session we just established.
        if self.module_info is None:
            try:
                self.module_info = await self.request_module_info()
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.debug("Could not read module info from %s: %s", self._host, err)

    async def disconnect(self) -> None:
        """Stop the supervisor and close the connection.

        Safe to call repeatedly, and safe while async_run() is waiting out a
        backoff delay - the wait ends immediately.
        """
        _LOGGER.debug("Disconnecting LAN session to %s", self._host)
        self._close_event.set()
        await self._teardown()

    async def request_status(self) -> dict[str, Any]:
        """Ask for a full status report and return the decoded attributes.

        The P0_READ byte is required. Without it the device says nothing at
        all - no error, no NAK - while still answering pings on the same
        connection, which is indistinguishable from a wrong command code.
        """
        frame = await self._request(CMD_STATUS, CMD_STATUS_RESPONSE, bytes([P0_READ]))
        return self._handle_status(frame)

    async def request_module_info(self) -> ModuleInfo | None:
        """Ask the wifi module to describe itself.

        Unlike a status request this needs no p0 action byte - it is a module
        command rather than a datapoint one, and the device answers a bare
        request.
        """
        frame = await self._request(CMD_MODULE_INFO, CMD_MODULE_INFO_RESPONSE)
        return parse_module_info(frame.payload)

    async def _serve(self) -> None:
        """Wait for the connection to end, surfacing why it did."""
        if self._receive_task is None:
            return
        try:
            await self._receive_task
        except asyncio.CancelledError:
            if self._close_event.is_set():
                # disconnect() cancelled it; that is an orderly stop, not a
                # cancellation of the supervisor itself.
                return
            raise

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    async def _fetch_passcode(self) -> bytes:
        """Ask the device for its passcode.

        It hands this out to anything on the LAN that asks, so it is not a
        secret in any strong sense - but it identifies the device, so it is
        never logged.
        """
        frame = await self._request(CMD_PASSCODE, CMD_PASSCODE_RESPONSE)
        if len(frame.payload) < 2:
            raise LanSessionError("passcode response was too short to parse")

        length = int.from_bytes(frame.payload[:2], "big")
        passcode = frame.payload[2 : 2 + length]
        if len(passcode) != length:
            raise LanSessionError(
                f"passcode response claims {length} bytes, carries {len(passcode)}"
            )

        _LOGGER.debug("Received a %d-byte passcode from %s", length, self._host)
        return passcode

    async def _login(self, passcode: bytes) -> None:
        """Authenticate with the passcode just obtained."""
        payload = len(passcode).to_bytes(2, "big") + passcode
        frame = await self._request(CMD_LOGIN, CMD_LOGIN_RESPONSE, payload)

        if not frame.payload:
            raise LanSessionError("login response carried no result byte")
        if frame.payload[0] != 0:
            raise LoginRefused(f"device refused login, code {frame.payload[0]}")

        _LOGGER.debug("Authenticated with %s", self._host)

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    async def _send(self, cmd: int, payload: bytes = b"") -> None:
        """Write one packet."""
        if self._writer is None:
            raise LanSessionError("cannot send, not connected")
        self._writer.write(pack(cmd, payload))
        await self._writer.drain()

    async def _request(self, cmd: int, expect: int, payload: bytes = b"") -> Frame:
        """Send a command and wait for the reply the device answers it with.

        The waiting happens against the receive loop rather than by reading the
        socket directly, so the ping task keeps running throughout. That is the
        whole reason this indirection exists.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Frame] = loop.create_future()
        self._waiters[expect] = future

        try:
            await self._send(cmd, payload)
            if self._receive_task is None:
                # Handshake: nothing is reading the socket yet, so read here.
                return await self._read_until(expect)
            async with asyncio.timeout(_REPLY_TIMEOUT):
                return await future
        except TimeoutError as err:
            raise LanSessionError(
                f"no reply to command 0x{cmd:04x} from {self._host}"
            ) from err
        finally:
            self._waiters.pop(expect, None)

    async def _read_until(self, expect: int) -> Frame:
        """Read directly from the socket until a given command arrives.

        Only used during the handshake, before the receive loop starts.
        """
        if self._reader is None:
            raise LanSessionError("cannot read, not connected")

        buffer = b""
        async with asyncio.timeout(_REPLY_TIMEOUT):
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    raise LanSessionError("device closed the connection")
                buffer += chunk
                try:
                    frames, buffer = split_stream(buffer)
                except FramingError as err:
                    raise LanSessionError(f"cannot parse stream: {err}") from err
                for frame in frames:
                    if frame.cmd == expect:
                        return frame

    async def _receive_loop(self) -> None:
        """Read frames until the connection drops.

        Returns rather than reconnecting; deciding whether to retry is the
        supervisor's job.
        """
        if self._reader is None:
            return

        buffer = b""
        while True:
            chunk = await self._reader.read(4096)
            if not chunk:
                _LOGGER.debug("Device %s closed the connection", self._host)
                return

            buffer += chunk
            try:
                frames, buffer = split_stream(buffer)
            except FramingError as err:
                # Unreadable bytes mean the stream is out of step; the
                # supervisor's reconnect is the honest way back.
                raise LanSessionError(f"cannot parse stream: {err}") from err

            for frame in frames:
                self._dispatch(frame)

    def _dispatch(self, frame: Frame) -> None:
        """Route one frame to whatever is waiting for it."""
        waiter = self._waiters.get(frame.cmd)
        if waiter is not None and not waiter.done():
            # Whoever asked for this decodes it; doing it here as well would
            # fire the update callback twice for one status.
            waiter.set_result(frame)
            return

        if frame.cmd == CMD_STATUS_RESPONSE:
            # Nobody asked - the device volunteered a state change.
            try:
                self._handle_status(frame)
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.error("Could not handle status from %s: %s", self._host, err)

    def _handle_status(self, frame: Frame) -> dict[str, Any]:
        """Decode a status frame and pass the attributes on."""
        if not frame.payload:
            raise LanSessionError("status frame carried no payload")

        # payload[0] is the p0 action byte; datapoints follow it.
        attrs = decode_attrs(self._schema, frame.payload[1:])
        try:
            self._update_callback(attrs)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("Error in LAN update callback: %s", err)
        return attrs

    async def _ping_loop(self) -> None:
        """Keep the session alive.

        Runs for as long as the connection does. Its absence during a request
        is what stops the device answering, so this is not optional decoration.
        """
        # No re-check after the sleep: _teardown() clears the flag and cancels
        # this task without awaiting in between, so it cannot wake up to a
        # connection that has already gone.
        try:
            while self._connected:
                await asyncio.sleep(_PING_INTERVAL)
                await self._send(CMD_PING)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pylint: disable=broad-except
            # Let the receive loop notice the connection is gone.
            _LOGGER.debug("Ping to %s failed: %s", self._host, err)

    async def _teardown(self) -> None:
        """Close everything down. Idempotent."""
        self._connected = False

        await self._stop(self._ping_task)
        self._ping_task = None
        await self._stop(self._receive_task)
        self._receive_task = None

        for waiter in self._waiters.values():
            if not waiter.done():
                waiter.set_exception(LanSessionError("connection closed"))
        self._waiters.clear()

        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _stop(task: asyncio.Task[None] | None) -> None:
        """Cancel a background task and wait for it to actually finish.

        The waiting is the point: a task left merely cancelled outlives the
        session it belongs to, and Home Assistant fails a config entry unload
        that leaves one behind.
        """
        if task is None:
            return
        if task is asyncio.current_task():
            # Tearing down from inside the task itself; awaiting would hang.
            return
        if not task.done():
            task.cancel()
        # Awaited even when already finished, so a failure that nobody else
        # collected does not surface later as a stray "never retrieved" error.
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("LAN background task ended: %s", err)

    def _retry_delay(self) -> int:
        """Pick the next backoff delay, holding at the last one thereafter."""
        delay = _RECONNECT_DELAYS[
            min(self._reconnect_count, len(_RECONNECT_DELAYS) - 1)
        ]
        self._reconnect_count += 1
        return delay

    async def _maybe_rediscover(self) -> None:
        """After repeated failures, ask where the spa went.

        A spa whose DHCP lease moves would otherwise be retried at its old
        address until someone noticed and edited the setting by hand - and the
        symptom is quiet, because the integration carries on over the cloud.

        Safe to do from here: looking the spa up is a UDP broadcast, which
        costs none of the device's small pool of connection slots, unlike the
        TCP attempts that got us here.

        Runs before every retry once the threshold is passed, not just once at
        it. A spa can be unreachable for an hour and come back somewhere else,
        and a single lookup on the way down would have been spent long before
        that. By then the backoff has stretched to a minute, so this is at
        most one broadcast a minute while the spa is missing.

        The new address is used for this run only and not written back to the
        configuration. Persisting it would reload the config entry, and a
        reload tears down and reopens the session - which is exactly the
        pattern that exhausts those connection slots if it ever happened
        repeatedly. Recovery runs again by itself after a restart, so the cost
        of not persisting is a few failed attempts, not a broken feature.
        """
        if self._address_resolver is None:
            return
        if self._reconnect_count < _REDISCOVER_AFTER_FAILURES:
            return

        _LOGGER.debug(
            "%d failed attempts to reach %s; looking for it on the network",
            self._reconnect_count,
            self._host,
        )
        try:
            address = await self._address_resolver()
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Could not look up the spa's address: %s", err)
            return

        if address is None:
            _LOGGER.debug("The spa did not answer; keeping %s", self._host)
            return
        if address == self._host:
            _LOGGER.debug("The spa is still at %s", self._host)
            return

        _LOGGER.info(
            "The spa has moved from %s to %s; using the new address. Update "
            "the address in the integration options to avoid this delay after "
            "a restart",
            self._host,
            address,
        )
        self._host = address

    async def _wait_before_retry(self) -> None:
        """Wait out the backoff, returning early on shutdown."""
        delay = self._retry_delay()

        _LOGGER.debug("Reconnecting to %s in %ds", self._host, delay)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(delay):
                await self._close_event.wait()

    @staticmethod
    def _notify(callback: Callable[[], None] | None, label: str) -> None:
        """Invoke a lifecycle callback without letting it break the session."""
        if callback is None:
            return
        try:
            callback()
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("Error in LAN %s callback: %s", label, err)
