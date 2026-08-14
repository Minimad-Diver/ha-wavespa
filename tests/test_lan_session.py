"""Tests for the LAN session, driven by a scripted fake device.

No hardware involved. The fake answers the same command sequence a real spa
does, so the handshake, keepalive and reconnect behaviour are pinned in CI
rather than only having been seen to work once by hand.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any

import pytest

from custom_components.wavespa.lan.codec import DatapointSchema
from custom_components.wavespa.lan.framing import (
    CMD_LOGIN_RESPONSE,
    CMD_PASSCODE_RESPONSE,
    CMD_PING,
    CMD_STATUS,
    CMD_STATUS_RESPONSE,
    pack,
    split_stream,
)
from custom_components.wavespa.lan.session import (
    GizwitsLanSession,
    LanSessionError,
    LoginRefused,
)

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "datapoint_wave_spa.json"

PASSCODE = b"JDBHEFKJYK"

# The live payload a real spa returned: p0 action byte then nine datapoints.
STATUS_PAYLOAD = bytes([0x04]) + bytes.fromhex("05180000001a001600")


@pytest.fixture(name="schema")
def schema_fixture() -> DatapointSchema:
    return DatapointSchema(json.loads(_FIXTURE.read_text(encoding="utf8")))


class FakeWriter:
    """Captures what the session sends, and can be inspected mid-test."""

    def __init__(self, device: FakeDevice) -> None:
        self._device = device
        self.closed = False
        self.sent: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.sent.append(data)
        self._device.feed_request(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        self._device.close()

    async def wait_closed(self) -> None:
        return None

    def commands_sent(self) -> list[int]:
        frames, _ = split_stream(b"".join(self.sent))
        return [f.cmd for f in frames]


class FakeDevice:
    """A spa that answers the way the real one does."""

    def __init__(
        self,
        *,
        login_result: int = 0,
        answer_status: bool = True,
        passcode: bytes = PASSCODE,
    ) -> None:
        self.reader = asyncio.StreamReader()
        self.writer = FakeWriter(self)
        self.login_result = login_result
        self.answer_status = answer_status
        self.passcode = passcode
        self.pings_received = 0
        self.status_requests = 0

    def feed_request(self, data: bytes) -> None:
        """Respond to whatever the session just sent."""
        frames, _ = split_stream(data)
        for frame in frames:
            if frame.cmd == 0x0006:
                body = len(self.passcode).to_bytes(2, "big") + self.passcode
                self._reply(CMD_PASSCODE_RESPONSE, body)
            elif frame.cmd == 0x0008:
                self._reply(CMD_LOGIN_RESPONSE, bytes([self.login_result]))
            elif frame.cmd == CMD_STATUS:
                self.status_requests += 1
                if self.answer_status:
                    self._reply(CMD_STATUS_RESPONSE, STATUS_PAYLOAD)
            elif frame.cmd == CMD_PING:
                self.pings_received += 1
                self._reply(0x0016)

    def _reply(self, cmd: int, payload: bytes = b"") -> None:
        self.reader.feed_data(pack(cmd, payload))

    def push_status(self, payload: bytes = STATUS_PAYLOAD) -> None:
        """Send an unsolicited status, as the device does on a state change."""
        self.reader.feed_data(pack(CMD_STATUS_RESPONSE, payload, flag=1))

    def close(self) -> None:
        self.reader.feed_eof()

    async def connector(self, host: str, port: int) -> tuple[asyncio.StreamReader, Any]:
        return self.reader, self.writer


def make_session(
    schema: DatapointSchema, device: FakeDevice, **kwargs: Any
) -> tuple[GizwitsLanSession, list[dict[str, Any]]]:
    """Build a session wired to a fake device, plus the updates it emits."""
    updates: list[dict[str, Any]] = []
    session = GizwitsLanSession(
        "192.0.2.10",
        schema,
        updates.append,
        connector=device.connector,
        **kwargs,
    )
    return session, updates


class TestHandshake:
    """Connect, passcode, login, first status - the sequence a real spa needs."""

    async def test_connect_succeeds(self, schema: DatapointSchema) -> None:
        device = FakeDevice()
        session, _ = make_session(schema, device)

        await session.connect()

        assert session.is_connected is True
        await session.disconnect()

    async def test_commands_are_sent_in_order(self, schema: DatapointSchema) -> None:
        """Passcode before login before status - the device requires it."""
        device = FakeDevice()
        session, _ = make_session(schema, device)

        await session.connect()
        assert device.writer.commands_sent()[:3] == [0x0006, 0x0008, CMD_STATUS]

        await session.disconnect()

    async def test_login_sends_the_passcode_length_prefixed(
        self, schema: DatapointSchema
    ) -> None:
        device = FakeDevice()
        session, _ = make_session(schema, device)

        await session.connect()
        frames, _ = split_stream(b"".join(device.writer.sent))
        login = next(f for f in frames if f.cmd == 0x0008)

        assert login.payload == len(PASSCODE).to_bytes(2, "big") + PASSCODE
        await session.disconnect()

    async def test_initial_status_primes_the_cache(
        self, schema: DatapointSchema
    ) -> None:
        """Entities should have state without waiting for a push."""
        device = FakeDevice()
        session, updates = make_session(schema, device)

        await session.connect()

        assert len(updates) == 1
        assert updates[0]["Heater"] == 1
        assert updates[0]["Current_temperature"] == 26
        await session.disconnect()

    async def test_refused_login_raises(self, schema: DatapointSchema) -> None:
        device = FakeDevice(login_result=1)
        session, _ = make_session(schema, device)

        with pytest.raises(LoginRefused, match="code 1"):
            await session.connect()

        assert session.is_connected is False

    async def test_short_passcode_response_raises(
        self, schema: DatapointSchema
    ) -> None:
        device = FakeDevice()
        device.feed_request = lambda data: device.reader.feed_data(  # type: ignore[method-assign]
            pack(CMD_PASSCODE_RESPONSE, b"\x00")
        )
        session, _ = make_session(schema, device)

        with pytest.raises(LanSessionError, match="too short"):
            await session.connect()

    async def test_truncated_passcode_raises(self, schema: DatapointSchema) -> None:
        """A length that overstates the bytes present must not be trusted."""
        device = FakeDevice()
        device.feed_request = lambda data: device.reader.feed_data(  # type: ignore[method-assign]
            pack(CMD_PASSCODE_RESPONSE, (99).to_bytes(2, "big") + b"short")
        )
        session, _ = make_session(schema, device)

        with pytest.raises(LanSessionError, match="claims 99"):
            await session.connect()

    async def test_connecting_twice_is_a_no_op(self, schema: DatapointSchema) -> None:
        device = FakeDevice()
        session, _ = make_session(schema, device)

        await session.connect()
        await session.connect()

        assert device.writer.commands_sent().count(0x0006) == 1
        await session.disconnect()


class TestKeepalive:
    """The rule that cost two failed experiments against real hardware."""

    async def test_pings_are_sent_while_the_session_is_open(
        self, schema: DatapointSchema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "custom_components.wavespa.lan.session._PING_INTERVAL", 0.01
        )
        device = FakeDevice()
        session, _ = make_session(schema, device)

        await session.connect()
        await asyncio.sleep(0.08)

        assert device.pings_received >= 2
        await session.disconnect()

    async def test_pings_continue_during_a_request(
        self, schema: DatapointSchema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole reason requests are correlated rather than read inline.

        A real spa stops answering, then drops the connection, if pings pause
        while a reply is awaited.
        """
        monkeypatch.setattr(
            "custom_components.wavespa.lan.session._PING_INTERVAL", 0.01
        )
        monkeypatch.setattr("custom_components.wavespa.lan.session._REPLY_TIMEOUT", 0.2)
        device = FakeDevice()
        session, _ = make_session(schema, device)
        await session.connect()

        device.answer_status = False  # the spa goes quiet
        pings_before = device.pings_received
        with pytest.raises(LanSessionError, match="no reply"):
            await session.request_status()

        assert device.pings_received > pings_before, (
            "pings stopped while waiting for a reply - the device would drop us"
        )
        await session.disconnect()

    async def test_ping_stops_after_disconnect(
        self, schema: DatapointSchema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "custom_components.wavespa.lan.session._PING_INTERVAL", 0.01
        )
        device = FakeDevice()
        session, _ = make_session(schema, device)

        await session.connect()
        await session.disconnect()
        settled = device.pings_received
        await asyncio.sleep(0.05)

        assert device.pings_received == settled


class TestStatusUpdates:
    """Both the answers we ask for and the ones the device volunteers."""

    async def test_requested_status_returns_attributes(
        self, schema: DatapointSchema
    ) -> None:
        device = FakeDevice()
        session, _ = make_session(schema, device)
        await session.connect()

        attrs = await session.request_status()

        assert attrs["Filter"] == 1
        assert attrs["Time_filter"] == 22
        await session.disconnect()

    async def test_unsolicited_push_updates_state(
        self, schema: DatapointSchema
    ) -> None:
        """A state change reaches us without being asked for."""
        device = FakeDevice()
        session, updates = make_session(schema, device)
        await session.connect()

        changed = bytes([0x04]) + bytes.fromhex("07180000001a001600")
        device.push_status(changed)
        await asyncio.sleep(0.05)

        assert updates[-1]["Bubble"] == 1
        await session.disconnect()

    async def test_a_failing_callback_does_not_break_the_session(
        self, schema: DatapointSchema
    ) -> None:
        device = FakeDevice()

        def explode(_attrs: dict[str, Any]) -> None:
            raise RuntimeError("boom")

        session = GizwitsLanSession(
            "192.0.2.10", schema, explode, connector=device.connector
        )

        await session.connect()

        assert session.is_connected is True
        await session.disconnect()

    async def test_empty_status_payload_is_rejected(
        self, schema: DatapointSchema
    ) -> None:
        device = FakeDevice()
        session, _ = make_session(schema, device)
        await session.connect()

        from custom_components.wavespa.lan.framing import Frame

        with pytest.raises(LanSessionError, match="no payload"):
            session._handle_status(Frame(cmd=CMD_STATUS_RESPONSE, payload=b""))

        await session.disconnect()


class TestSupervisor:
    """Reconnect behaviour, mirroring the WebSocket client."""

    async def test_disconnect_stops_the_supervisor(
        self, schema: DatapointSchema
    ) -> None:
        device = FakeDevice()
        session, _ = make_session(schema, device)

        task = asyncio.create_task(session.async_run())
        await asyncio.sleep(0.05)
        await session.disconnect()

        async with asyncio.timeout(2):
            await task
        assert session.is_connected is False

    async def test_callbacks_fire_on_connect_and_loss(
        self, schema: DatapointSchema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "custom_components.wavespa.lan.session._RECONNECT_DELAYS", (0.01,)
        )
        device = FakeDevice()
        events: list[str] = []
        session, _ = make_session(
            schema,
            device,
            connect_callback=lambda: events.append("up"),
            disconnect_callback=lambda: events.append("down"),
        )

        task = asyncio.create_task(session.async_run())
        await asyncio.sleep(0.05)
        device.close()  # device hangs up
        await asyncio.sleep(0.05)
        await session.disconnect()
        async with asyncio.timeout(2):
            await task

        assert events[0] == "up"
        assert "down" in events

    async def test_a_failing_callback_does_not_stop_the_supervisor(
        self, schema: DatapointSchema
    ) -> None:
        device = FakeDevice()
        session, _ = make_session(schema, device, connect_callback=lambda: 1 / 0)

        task = asyncio.create_task(session.async_run())
        await asyncio.sleep(0.05)
        await session.disconnect()
        async with asyncio.timeout(2):
            await task

    async def test_teardown_wakes_pending_requests(
        self, schema: DatapointSchema
    ) -> None:
        """A request in flight when the connection dies must not hang."""
        device = FakeDevice()
        session, _ = make_session(schema, device)
        await session.connect()

        device.answer_status = False
        pending = asyncio.create_task(session.request_status())
        await asyncio.sleep(0)
        await session.disconnect()

        with pytest.raises(LanSessionError):
            async with asyncio.timeout(2):
                await pending


class TestFaults:
    """Ways a connection goes wrong that a spa on a home network really does."""

    async def test_empty_login_response_raises(self, schema: DatapointSchema) -> None:
        device = FakeDevice()
        session, _ = make_session(schema, device)
        original = device.feed_request

        def truncate(data: bytes) -> None:
            frames, _ = split_stream(data)
            if any(f.cmd == 0x0008 for f in frames):
                device.reader.feed_data(pack(CMD_LOGIN_RESPONSE, b""))
                return
            original(data)

        device.feed_request = truncate  # type: ignore[method-assign]

        with pytest.raises(LanSessionError, match="no result byte"):
            await session.connect()

    async def test_garbage_during_handshake_raises(
        self, schema: DatapointSchema
    ) -> None:
        """A device speaking something else entirely must not look like silence."""
        device = FakeDevice()
        device.feed_request = lambda data: device.reader.feed_data(b"not a packet")  # type: ignore[method-assign]
        session, _ = make_session(schema, device)

        with pytest.raises(LanSessionError, match="cannot parse"):
            await session.connect()

    async def test_hangup_during_handshake_raises(
        self, schema: DatapointSchema
    ) -> None:
        device = FakeDevice()
        device.feed_request = lambda data: device.reader.feed_eof()  # type: ignore[method-assign]
        session, _ = make_session(schema, device)

        with pytest.raises(LanSessionError, match="closed the connection"):
            await session.connect()

    async def test_a_failed_handshake_closes_the_socket(
        self, schema: DatapointSchema
    ) -> None:
        """Otherwise every retry would leak a half-open connection."""
        device = FakeDevice(login_result=1)
        session, _ = make_session(schema, device)

        with pytest.raises(LoginRefused):
            await session.connect()

        assert device.writer.closed is True

    async def test_unparsable_stream_ends_the_connection(
        self, schema: DatapointSchema
    ) -> None:
        """Out-of-step bytes cannot be resynchronised; reconnecting is honest."""
        device = FakeDevice()
        session, _ = make_session(schema, device)
        await session.connect()

        device.reader.feed_data(b"\xff\xff\xff\xff\xff\xff\xff\xff")
        await asyncio.sleep(0.05)

        assert session._receive_task is not None
        with pytest.raises(LanSessionError, match="cannot parse"):
            await session._receive_task

        await session.disconnect()

    async def test_a_corrupt_push_does_not_kill_the_session(
        self, schema: DatapointSchema
    ) -> None:
        """One bad status should not take the integration offline."""
        device = FakeDevice()
        session, updates = make_session(schema, device)
        await session.connect()

        device.push_status(bytes([0x04]))  # p0 byte, then nothing
        await asyncio.sleep(0.05)

        assert session.is_connected is True
        assert len(updates) == 1  # only the priming status got through
        await session.disconnect()

    async def test_a_failing_ping_is_swallowed(
        self, schema: DatapointSchema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The receive loop notices the drop; the ping task need not shout."""
        monkeypatch.setattr(
            "custom_components.wavespa.lan.session._PING_INTERVAL", 0.01
        )
        device = FakeDevice()
        session, _ = make_session(schema, device)
        await session.connect()

        def refuse(_data: bytes) -> None:
            raise OSError("network unreachable")

        device.writer.write = refuse  # type: ignore[assignment]
        await asyncio.sleep(0.05)

        assert session._ping_task is not None
        assert session._ping_task.done()
        await session.disconnect()


class TestBackoff:
    """Retry pacing, which mirrors the WebSocket client's schedule."""

    def test_delays_escalate_then_hold(self, schema: DatapointSchema) -> None:
        """Past the end of the schedule it must hold, not raise IndexError."""
        session, _ = make_session(schema, FakeDevice())

        assert [session._retry_delay() for _ in range(8)] == [
            3,
            6,
            12,
            24,
            48,
            60,
            60,
            60,
        ]

    async def test_a_successful_connect_resets_the_backoff(
        self, schema: DatapointSchema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A long outage must not leave a later blip waiting a minute."""
        monkeypatch.setattr(
            "custom_components.wavespa.lan.session._RECONNECT_DELAYS", (0.01,)
        )
        device = FakeDevice()
        session, _ = make_session(schema, device)
        session._reconnect_count = 5

        task = asyncio.create_task(session.async_run())
        await asyncio.sleep(0.05)
        assert session._reconnect_count == 0

        await session.disconnect()
        async with asyncio.timeout(2):
            await task

    async def test_a_shutdown_ends_the_backoff_wait_immediately(
        self, schema: DatapointSchema
    ) -> None:
        """Otherwise unloading the integration would block for up to a minute."""
        session, _ = make_session(schema, FakeDevice())
        session._close_event.set()

        async with asyncio.timeout(2):
            await session._wait_before_retry()


class TestSendGuards:
    """Operations on a session that is not connected."""

    async def test_send_without_a_connection_raises(
        self, schema: DatapointSchema
    ) -> None:
        device = FakeDevice()
        session, _ = make_session(schema, device)

        with pytest.raises(LanSessionError, match="not connected"):
            await session._send(CMD_PING)

    async def test_read_without_a_connection_raises(
        self, schema: DatapointSchema
    ) -> None:
        device = FakeDevice()
        session, _ = make_session(schema, device)

        with pytest.raises(LanSessionError, match="not connected"):
            await session._read_until(CMD_STATUS_RESPONSE)

    async def test_receive_loop_without_a_connection_returns(
        self, schema: DatapointSchema
    ) -> None:
        device = FakeDevice()
        session, _ = make_session(schema, device)

        await session._receive_loop()  # must not raise

    async def test_serve_without_a_connection_returns(
        self, schema: DatapointSchema
    ) -> None:
        device = FakeDevice()
        session, _ = make_session(schema, device)

        await session._serve()  # must not raise
