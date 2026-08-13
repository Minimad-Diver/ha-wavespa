"""Tests for the Gizwits WebSocket module."""

import asyncio
import json
import sys
from types import FrameType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets.exceptions

from custom_components.wavespa.wavespa.websocket import (
    GizwitsWebSocket,
    GizwitsWebSocketException,
)


def _stack_depth() -> int:
    """Return the current call stack depth, without touching the filesystem."""
    depth = 0
    frame: FrameType | None = sys._getframe()
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return depth


def _make_ws(**kwargs) -> GizwitsWebSocket:
    """Build a client with sensible defaults for the fields under test."""
    kwargs.setdefault("uid", "test_uid")
    kwargs.setdefault("token", "test_token")
    kwargs.setdefault("ws_host", "m2m.gizwits.com")
    kwargs.setdefault("ws_port", 8880)
    kwargs.setdefault("update_callback", MagicMock())
    return GizwitsWebSocket(**kwargs)


@pytest.mark.asyncio
async def test_websocket_connect_success():
    """Test successful WebSocket connection and authentication."""
    ws = _make_ws(uid="test_uid_123", token="test_token_abc")

    # Should not be connected initially
    assert not ws.is_connected

    with patch(
        "custom_components.wavespa.wavespa.websocket.websockets.connect"
    ) as mock_connect:
        with patch("homeassistant.util.ssl.get_default_context") as mock_ssl:
            mock_ws = MagicMock()
            mock_ws.send = AsyncMock()
            mock_ws.close = AsyncMock()
            mock_ssl.return_value = MagicMock()

            # Make connect return coroutine
            async def mock_connect_coro(*args, **kwargs):
                return mock_ws

            mock_connect.side_effect = mock_connect_coro

            # Mock successful login response
            mock_ws.recv = AsyncMock(
                return_value=json.dumps({"cmd": "login_res", "data": {"success": True}})
            )

            await ws.connect()

            # Verify connection established
            assert ws.is_connected

            # Verify connection called with correct URL
            mock_connect.assert_called_once()
            call_args = mock_connect.call_args[0]
            assert call_args[0] == "wss://m2m.gizwits.com:8880/ws/app/v1"

            # Verify login message sent
            mock_ws.send.assert_called_once()
            login_msg = json.loads(mock_ws.send.call_args[0][0])
            assert login_msg["cmd"] == "login_req"
            assert login_msg["data"]["uid"] == "test_uid_123"
            assert login_msg["data"]["token"] == "test_token_abc"
            assert login_msg["data"]["auto_subscribe"] is True

            # Cleanup
            await ws.disconnect()


@pytest.mark.asyncio
async def test_websocket_login_failure():
    """Test that a refused login is raised for the supervisor to handle."""
    ws = _make_ws(uid="bad_uid", token="bad_token")

    with patch(
        "custom_components.wavespa.wavespa.websocket.websockets.connect"
    ) as mock_connect:
        with patch("homeassistant.util.ssl.get_default_context") as mock_ssl:
            mock_ws = MagicMock()
            mock_ws.send = AsyncMock()
            mock_ws.close = AsyncMock()
            mock_ssl.return_value = MagicMock()

            async def mock_connect_coro(*args, **kwargs):
                return mock_ws

            mock_connect.side_effect = mock_connect_coro

            # Mock failed login response
            mock_ws.recv = AsyncMock(
                return_value=json.dumps(
                    {
                        "cmd": "login_res",
                        "data": {"success": False, "msg": "Invalid credentials"},
                    }
                )
            )

            with pytest.raises(GizwitsWebSocketException, match="Invalid credentials"):
                await ws.connect()

            assert not ws.is_connected


@pytest.mark.asyncio
async def test_websocket_connection_error():
    """Test that a transport failure is raised for the supervisor to handle."""
    ws = _make_ws()

    with patch(
        "custom_components.wavespa.wavespa.websocket.websockets.connect"
    ) as mock_connect:
        with patch("homeassistant.util.ssl.get_default_context"):

            async def mock_connect_error(*args, **kwargs):
                raise OSError("Connection refused")

            mock_connect.side_effect = mock_connect_error

            with pytest.raises(OSError, match="Connection refused"):
                await ws.connect()

            assert not ws.is_connected


@pytest.mark.asyncio
async def test_async_run_retries_without_recursion():
    """Test that repeated failures don't grow the call stack.

    The previous implementation reconnected by calling connect() from inside
    the failure handler, so every retry added frames that never unwound. A
    sustained outage eventually hit RecursionError.
    """
    ws = _make_ws()
    depths: list[int] = []

    async def failing_connect():
        depths.append(_stack_depth())
        if len(depths) >= 5:
            ws._close_event.set()
        raise GizwitsWebSocketException("Connection refused")

    with patch.object(ws, "connect", side_effect=failing_connect):
        with patch.object(ws, "_wait_before_retry", new=AsyncMock()):
            await ws.async_run()

    assert len(depths) == 5
    assert depths == [depths[0]] * 5


@pytest.mark.asyncio
async def test_async_run_exits_when_disconnected():
    """Test that disconnect() stops the supervisor while it retries."""
    ws = _make_ws()

    with patch.object(ws, "connect", new=AsyncMock(side_effect=OSError("down"))):
        with patch(
            "custom_components.wavespa.wavespa.websocket._RECONNECT_DELAYS", [0.01]
        ):
            task = asyncio.create_task(ws.async_run())

            # Let it fail and retry a few times
            await asyncio.sleep(0.05)
            assert not task.done()

            await ws.disconnect()
            await asyncio.wait_for(task, timeout=1)

    assert not ws.is_connected


@pytest.mark.asyncio
async def test_disconnect_interrupts_backoff():
    """Test that shutdown doesn't have to wait out the backoff delay."""
    ws = _make_ws()
    ws._reconnect_count = 10  # Would otherwise wait the full 60 seconds

    task = asyncio.create_task(ws._wait_before_retry())
    await asyncio.sleep(0)  # Let the wait start

    await ws.disconnect()
    await asyncio.wait_for(task, timeout=1)


def test_backoff_delays_follow_schedule():
    """Test exponential backoff delays, capped at the maximum."""
    ws = _make_ws()

    delays = [ws._next_delay() for _ in range(8)]

    assert delays == [3, 6, 12, 24, 48, 60, 60, 60]
    assert ws._reconnect_count == 8


@pytest.mark.asyncio
async def test_async_run_resets_backoff_after_success():
    """Test that a successful connection restarts the backoff schedule."""
    ws = _make_ws()
    ws._reconnect_count = 4  # Pretend we've been retrying for a while

    async def listen_once():
        ws._close_event.set()

    with patch.object(ws, "connect", new=AsyncMock()):
        with patch.object(ws, "_listen_loop", side_effect=listen_once):
            await ws.async_run()

    assert ws._reconnect_count == 0


@pytest.mark.asyncio
async def test_disconnect_callback_only_on_unexpected_loss():
    """Test the disconnect callback fires on a dropped connection, not on shutdown."""
    disconnect_callback = MagicMock()
    ws = _make_ws(disconnect_callback=disconnect_callback)

    drops = []

    async def listen_then_close():
        drops.append(1)
        if len(drops) >= 2:
            # Second time round, simulate a deliberate shutdown
            ws._close_event.set()

    with patch.object(ws, "connect", new=AsyncMock()):
        with patch.object(ws, "_listen_loop", side_effect=listen_then_close):
            with patch.object(ws, "_wait_before_retry", new=AsyncMock()):
                await ws.async_run()

    assert len(drops) == 2
    # Only the first drop was unexpected
    disconnect_callback.assert_called_once()


@pytest.mark.asyncio
async def test_disconnect_callback_exception_handling():
    """Test that a failing disconnect callback doesn't stop the supervisor."""
    ws = _make_ws(disconnect_callback=MagicMock(side_effect=Exception("boom")))

    # Should not raise
    ws._notify_disconnected()


@pytest.mark.asyncio
async def test_connect_callback_fires_on_every_reconnect():
    """Test the connect callback announces recovery, not just the first connect.

    Only firing this once meant a single dropped connection left the
    coordinator on its disconnected fallback polling interval for the rest of
    the entry's life, because nothing ever told it the feed had come back.
    """
    connect_callback = MagicMock()
    ws = _make_ws(connect_callback=connect_callback)

    drops: list[int] = []

    async def listen_then_drop():
        drops.append(1)
        if len(drops) >= 3:
            ws._close_event.set()

    with patch.object(ws, "connect", new=AsyncMock()):
        with patch.object(ws, "_listen_loop", side_effect=listen_then_drop):
            with patch.object(ws, "_wait_before_retry", new=AsyncMock()):
                await ws.async_run()

    # Connected three times, so the coordinator heard about it three times
    assert connect_callback.call_count == 3


@pytest.mark.asyncio
async def test_connect_callback_not_fired_when_connect_fails():
    """Test a failed connection attempt doesn't claim the feed is live."""
    connect_callback = MagicMock()
    ws = _make_ws(connect_callback=connect_callback)

    attempts: list[int] = []

    async def failing_connect():
        attempts.append(1)
        if len(attempts) >= 3:
            ws._close_event.set()
        raise OSError("Connection refused")

    with patch.object(ws, "connect", side_effect=failing_connect):
        with patch.object(ws, "_wait_before_retry", new=AsyncMock()):
            await ws.async_run()

    connect_callback.assert_not_called()


@pytest.mark.asyncio
async def test_connect_callback_exception_handling():
    """Test that a failing connect callback doesn't stop the supervisor."""
    ws = _make_ws(connect_callback=MagicMock(side_effect=Exception("boom")))

    # Should not raise
    ws._notify_connected()


@pytest.mark.asyncio
async def test_websocket_device_update():
    """Test device update message handling."""
    updates_received = []

    def update_callback(device_id, attrs):
        updates_received.append((device_id, attrs))

    ws = _make_ws(update_callback=update_callback)

    # Test the message handler directly
    device_update_msg = {
        "cmd": "s2c_noti",
        "data": {
            "did": "device_abc123",
            "attrs": {
                "power": 1,
                "temp_now": 36,
                "temp_set": 38,
                "heat_power": 1,
                "filter_power": 1,
            },
        },
    }

    ws._handle_device_update(device_update_msg)

    # Verify callback was invoked
    assert len(updates_received) == 1
    assert updates_received[0][0] == "device_abc123"
    assert updates_received[0][1]["power"] == 1
    assert updates_received[0][1]["temp_now"] == 36


@pytest.mark.asyncio
async def test_websocket_device_update_empty_attrs():
    """Test device update with empty attributes."""
    update_callback = MagicMock()
    ws = _make_ws(update_callback=update_callback)

    # Device update with empty attrs
    device_update_msg = {"cmd": "s2c_noti", "data": {"did": "device_123", "attrs": {}}}

    ws._handle_device_update(device_update_msg)

    # Callback should not be invoked for empty attrs
    update_callback.assert_not_called()


@pytest.mark.asyncio
async def test_websocket_device_update_missing_device_id():
    """Test device update without device ID."""
    update_callback = MagicMock()
    ws = _make_ws(update_callback=update_callback)

    # Device update without did
    device_update_msg = {"cmd": "s2c_noti", "data": {"attrs": {"power": 1}}}

    ws._handle_device_update(device_update_msg)

    # Callback should not be invoked
    update_callback.assert_not_called()


@pytest.mark.asyncio
async def test_websocket_callback_exception_handling():
    """Test that exceptions in callback don't crash WebSocket."""

    def failing_callback(device_id, attrs):
        raise Exception("Callback error")

    ws = _make_ws(update_callback=failing_callback)

    # Should not raise exception
    device_update_msg = {
        "cmd": "s2c_noti",
        "data": {"did": "device_123", "attrs": {"power": 1}},
    }

    ws._handle_device_update(device_update_msg)
    # Test passes if no exception raised


@pytest.mark.asyncio
async def test_websocket_disconnect():
    """Test graceful disconnection."""
    ws = _make_ws()

    with patch(
        "custom_components.wavespa.wavespa.websocket.websockets.connect"
    ) as mock_connect:
        with patch("homeassistant.util.ssl.get_default_context"):
            mock_ws = MagicMock()
            mock_ws.send = AsyncMock()
            mock_ws.close = AsyncMock()

            async def mock_connect_coro(*args, **kwargs):
                return mock_ws

            mock_connect.side_effect = mock_connect_coro

            # Mock successful login
            mock_ws.recv = AsyncMock(
                return_value=json.dumps({"cmd": "login_res", "data": {"success": True}})
            )

            await ws.connect()

            # Verify connected
            assert ws.is_connected

            # Disconnect
            await ws.disconnect()

            # Verify disconnected
            assert not ws.is_connected
            mock_ws.close.assert_called_once()

            # Safe to call again
            await ws.disconnect()
            mock_ws.close.assert_called_once()


@pytest.mark.asyncio
async def test_websocket_already_connected():
    """Test connect() when WebSocket already connected."""
    ws = _make_ws()

    with patch(
        "custom_components.wavespa.wavespa.websocket.websockets.connect"
    ) as mock_connect:
        with patch("homeassistant.util.ssl.get_default_context"):
            mock_ws = MagicMock()
            mock_ws.send = AsyncMock()
            mock_ws.close = AsyncMock()

            async def mock_connect_coro(*args, **kwargs):
                return mock_ws

            mock_connect.side_effect = mock_connect_coro

            mock_ws.recv = AsyncMock(
                return_value=json.dumps({"cmd": "login_res", "data": {"success": True}})
            )

            await ws.connect()

            # Try to connect again
            await ws.connect()

            # Should only connect once (second call exits early)
            assert mock_connect.call_count == 1

            await ws.disconnect()


def test_device_ids_are_masked_in_logs():
    """api.py masks device IDs before logging; this module must agree.

    Debug logs are what people paste into bug reports, so masking in one module
    and not the other defeats the point.
    """
    from custom_components.wavespa.wavespa.websocket import _mask

    assert _mask("abcdef123456") == "***3456"
    assert _mask(None) == "unknown"
    assert _mask("") == "unknown"
    # Enough to correlate lines per device, not enough to identify the device
    assert "abcdef" not in _mask("abcdef123456")


def test_heartbeat_pings_within_the_declared_interval():
    """Pinging exactly on the deadline left no room for latency."""
    from custom_components.wavespa.wavespa import websocket as ws_module

    assert ws_module._HEARTBEAT_PING_SECONDS < ws_module._HEARTBEAT_INTERVAL_SECONDS
    assert ws_module._HEARTBEAT_PING_SECONDS == 90


@pytest.mark.asyncio
async def test_login_declares_the_heartbeat_interval_it_honours():
    """The interval sent to the server must match the constant we ping against."""
    from custom_components.wavespa.wavespa import websocket as ws_module

    ws = _make_ws()
    mock_ws = MagicMock()
    mock_ws.send = AsyncMock()
    ws._websocket = mock_ws

    await ws._send_login()

    login_msg = json.loads(mock_ws.send.call_args[0][0])
    assert (
        login_msg["data"]["heartbeat_interval"] == ws_module._HEARTBEAT_INTERVAL_SECONDS
    )


@pytest.mark.asyncio
async def test_heartbeat_loop_sends_ping():
    """Test heartbeat loop sends ping messages."""
    ws = _make_ws()

    mock_ws = MagicMock()
    mock_ws.send = AsyncMock()
    ws._websocket = mock_ws
    ws._connected = True

    # Test heartbeat sends ping
    with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        # Run one iteration of heartbeat loop
        mock_sleep.side_effect = [
            None,
            asyncio.CancelledError(),
        ]  # First sleep succeeds, second cancels

        try:
            await ws._heartbeat_loop()
        except asyncio.CancelledError:
            pass

        # Verify ping was sent
        assert mock_ws.send.called
        ping_msg = json.loads(mock_ws.send.call_args[0][0])
        assert ping_msg["cmd"] == "ping"


@pytest.mark.asyncio
async def test_listen_loop_processes_messages():
    """Test listen loop processes incoming messages."""
    updates_received = []

    def update_callback(device_id, attrs):
        updates_received.append((device_id, attrs))

    ws = _make_ws(update_callback=update_callback)

    # Mock WebSocket with messages
    mock_ws = MagicMock()
    ws._websocket = mock_ws

    # Create async iterator that yields messages then stops
    messages = [
        json.dumps(
            {"cmd": "s2c_noti", "data": {"did": "device1", "attrs": {"power": 1}}}
        ),
        json.dumps({"cmd": "pong"}),
        json.dumps(
            {"cmd": "s2c_online_status", "data": {"did": "device1", "is_online": True}}
        ),
    ]

    async def mock_async_iter():
        for msg in messages:
            yield msg

    mock_ws.__aiter__ = lambda self: mock_async_iter()

    # Run listen loop (will process messages then exit)
    await ws._listen_loop()

    # Verify device update was processed
    assert len(updates_received) == 1
    assert updates_received[0][0] == "device1"
    assert updates_received[0][1]["power"] == 1


@pytest.mark.asyncio
async def test_listen_loop_handles_json_decode_error():
    """Test listen loop handles malformed JSON gracefully."""
    update_callback = MagicMock()
    ws = _make_ws(update_callback=update_callback)

    mock_ws = MagicMock()
    ws._websocket = mock_ws

    # Send malformed JSON
    async def mock_async_iter():
        yield "{ invalid json }"

    mock_ws.__aiter__ = lambda self: mock_async_iter()

    # Should not raise exception
    await ws._listen_loop()

    # Callback should not have been called
    update_callback.assert_not_called()


@pytest.mark.asyncio
async def test_listen_loop_handles_connection_closed():
    """Test listen loop returns when the connection closes."""
    ws = _make_ws()

    mock_ws = MagicMock()
    ws._websocket = mock_ws
    ws._connected = True

    # Simulate ConnectionClosed exception
    async def mock_async_iter():
        if False:
            yield
        raise websockets.exceptions.ConnectionClosed(None, None)

    mock_ws.__aiter__ = lambda self: mock_async_iter()

    # Returns normally - reconnecting is the supervisor's decision, not the
    # listen loop's
    await ws._listen_loop()
