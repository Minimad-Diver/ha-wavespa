"""Gizwits WebSocket client for real-time device updates."""

import asyncio
import contextlib
import json
from logging import getLogger
from typing import Any, Callable

import websockets

from ..const import GIZWITS_APP_ID

_LOGGER = getLogger(__name__)

# Reconnection delays (exponential backoff): 3s → 6s → 12s → 24s → 48s → 60s max
_RECONNECT_DELAYS = [3, 6, 12, 24, 48, 60]


class GizwitsWebSocketException(Exception):
    """Base exception for WebSocket operations."""


class GizwitsWebSocket:
    """Gizwits WebSocket client for real-time device updates.

    Connects to Gizwits IoT platform WebSocket API to receive real-time
    device status updates via push notifications. Implements automatic
    reconnection with exponential backoff and graceful error handling.

    The WebSocket URL and port are extracted from the device bindings API
    response, allowing regional endpoint support without hardcoding.
    """

    def __init__(
        self,
        uid: str,
        token: str,
        ws_host: str,
        ws_port: int,
        update_callback: Callable[[str, dict[str, Any]], None],
        disconnect_callback: Callable[[], None] | None = None,
    ) -> None:
        """Initialize WebSocket client.

        Args:
            uid: User ID from Gizwits login API
            token: User token from Gizwits login API
            ws_host: WebSocket hostname (from device bindings response)
            ws_port: WebSocket port (from device bindings response)
            update_callback: Called with (device_id, attrs) on device updates
            disconnect_callback: Called on connection loss (optional)
        """
        self._uid = uid
        self._token = token
        self._ws_url = f"wss://{ws_host}:{ws_port}/ws/app/v1"
        self._update_callback = update_callback
        self._disconnect_callback = disconnect_callback

        self._websocket: Any = None
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._close_event = asyncio.Event()
        self._connected = False
        self._authenticated = False
        self._reconnect_count = 0

    async def async_run(self) -> None:
        """Keep a WebSocket connection alive until disconnect() is called.

        This is the entry point for normal use, and is meant to be awaited as
        a long-lived background task. Each pass round the loop makes a single
        connection attempt and then listens on it until the connection drops;
        failures are retried with exponential backoff. Because retries happen
        in the loop rather than by re-entering connect(), the call stack stays
        flat no matter how long an outage lasts.
        """
        self._close_event.clear()

        while not self._close_event.is_set():
            was_connected = False

            try:
                await self.connect()
                was_connected = True
                self._reconnect_count = 0
                await self._listen_loop()
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.warning("WebSocket connection attempt failed: %s", ex)
            finally:
                await self._teardown()

            if self._close_event.is_set():
                break

            if was_connected:
                self._notify_disconnected()

            await self._wait_before_retry()

        _LOGGER.debug("WebSocket supervisor stopped")

    async def connect(self) -> None:
        """Make a single connection attempt and authenticate.

        Establishes an SSL connection to the Gizwits WebSocket API, sends the
        login message, and starts the heartbeat task. Returns as soon as the
        connection is live - use async_run() for a connection that survives
        network drops.

        Raises:
            GizwitsWebSocketException: If authentication is refused
            Exception: Any transport error raised while connecting
        """
        if self._connected:
            _LOGGER.warning("WebSocket already connected")
            return

        _LOGGER.debug("Connecting to Gizwits WebSocket: %s", self._ws_url)

        # Use Home Assistant's pre-cached SSL context (avoids blocking warnings)
        # Import here to avoid circular dependency
        from homeassistant.util import ssl as ssl_util

        ssl_context = ssl_util.get_default_context()

        # Connect with SSL (Gizwits requires secure connection)
        websocket = await websockets.connect(
            self._ws_url,
            ssl=ssl_context,
            ping_interval=30,  # Keep connection alive
            ping_timeout=10,
        )
        self._websocket = websocket

        _LOGGER.debug("WebSocket connected, sending login")

        # Send login message
        await self._send_login()

        # Wait for login response with timeout
        response = await asyncio.wait_for(websocket.recv(), timeout=10)

        data = json.loads(response)
        if data.get("cmd") != "login_res":
            raise GizwitsWebSocketException(
                f"Expected login_res, got: {data.get('cmd')}"
            )

        if not data.get("data", {}).get("success"):
            error_msg = data.get("data", {}).get("msg", "Unknown error")
            raise GizwitsWebSocketException(f"Login failed: {error_msg}")

        self._authenticated = True
        self._connected = True

        _LOGGER.debug("WebSocket authenticated successfully")

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def disconnect(self) -> None:
        """Stop the supervisor and close the connection.

        Safe to call multiple times, and safe to call while async_run() is
        waiting out a backoff delay - the wait returns immediately.
        """
        _LOGGER.debug("Disconnecting WebSocket")
        self._close_event.set()
        await self._teardown()

    async def _teardown(self) -> None:
        """Cancel the heartbeat and close the socket.

        Idempotent, so the supervisor can call it after every attempt without
        having to know how far that attempt got.
        """
        self._connected = False
        self._authenticated = False

        heartbeat_task, self._heartbeat_task = self._heartbeat_task, None
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

        websocket, self._websocket = self._websocket, None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.debug("Error closing WebSocket: %s", ex)

    async def _wait_before_retry(self) -> None:
        """Wait out the backoff delay, returning early if we're shutting down.

        Implements exponential backoff:
        - Attempt 1: 3 seconds
        - Attempt 2: 6 seconds
        - Attempt 3: 12 seconds
        - Attempt 4: 24 seconds
        - Attempt 5: 48 seconds
        - Attempt 6+: 60 seconds (maximum delay)
        """
        delay = self._next_delay()

        _LOGGER.debug(
            "Reconnecting to WebSocket in %d seconds (attempt %d)",
            delay,
            self._reconnect_count,
        )

        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(delay):
                await self._close_event.wait()

    def _next_delay(self) -> int:
        """Return the delay for the next attempt, advancing the attempt count."""
        delay = _RECONNECT_DELAYS[
            min(self._reconnect_count, len(_RECONNECT_DELAYS) - 1)
        ]
        self._reconnect_count += 1
        return delay

    def _notify_disconnected(self) -> None:
        """Tell the coordinator the real-time feed has dropped."""
        if self._disconnect_callback is None:
            return

        try:
            self._disconnect_callback()
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("Error in disconnect callback: %s", ex)

    async def _send_login(self) -> None:
        """Send login message to authenticate WebSocket connection.

        Uses Gizwits' login_req protocol with auto_subscribe enabled to
        automatically receive updates for all bound devices.
        """
        login_msg = {
            "cmd": "login_req",
            "data": {
                "appid": GIZWITS_APP_ID,
                "uid": self._uid,
                "token": self._token,
                "p0_type": "attrs_v4",  # Use attributes protocol
                "heartbeat_interval": 180,  # Send heartbeat every 180 seconds
                "auto_subscribe": True,  # Subscribe to all bound devices
            },
        }

        if self._websocket is not None:
            await self._websocket.send(json.dumps(login_msg))
            _LOGGER.debug("Login message sent")

    async def _heartbeat_loop(self) -> None:
        """Send application-level heartbeat to keep connection alive.

        Gizwits requires explicit ping/pong messages every 180 seconds
        to maintain the connection. This is separate from WebSocket
        protocol-level ping/pong frames.
        """
        while self._connected:
            try:
                await asyncio.sleep(180)  # Wait 3 minutes

                if self._connected and self._websocket is not None:
                    # Send application-level ping
                    await self._websocket.send(json.dumps({"cmd": "ping"}))
                    _LOGGER.debug("Heartbeat ping sent")

            except asyncio.CancelledError:
                break
            except Exception as ex:
                _LOGGER.warning("Heartbeat failed: %s", ex)
                # Don't break on heartbeat failure - let listen_loop detect connection issues
                break

    async def _listen_loop(self) -> None:
        """Listen for incoming WebSocket messages until the connection drops.

        Returns once the connection closes for any reason; deciding whether to
        reconnect is the supervisor's job, not this loop's.
        """
        if self._websocket is None:
            return

        try:
            async for message in self._websocket:
                try:
                    data = json.loads(message)
                    cmd = data.get("cmd")

                    _LOGGER.debug("Received message: cmd=%s", cmd)

                    if cmd == "s2c_noti":
                        # Device status update notification
                        self._handle_device_update(data)

                    elif cmd == "s2c_online_status":
                        # Device online/offline notification
                        device_data = data.get("data", {})
                        device_id = device_data.get("did")
                        is_online = device_data.get("is_online")
                        _LOGGER.debug(
                            "Device %s is now %s",
                            device_id if device_id else "unknown",
                            "online" if is_online else "offline",
                        )

                    elif cmd == "s2c_invalid_msg":
                        # Invalid message error from server
                        _LOGGER.warning("Server reported invalid message: %s", data)

                    elif cmd == "pong":
                        # Heartbeat response
                        _LOGGER.debug("Heartbeat pong received")

                except json.JSONDecodeError:
                    _LOGGER.error("Failed to decode WebSocket message")
                except Exception as ex:
                    _LOGGER.error("Error processing WebSocket message: %s", ex)

        except websockets.exceptions.ConnectionClosed:
            _LOGGER.warning("WebSocket connection closed unexpectedly")
        except asyncio.CancelledError:
            _LOGGER.debug("WebSocket listen loop cancelled")
            raise
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("WebSocket listen error: %s", ex)

    def _handle_device_update(self, data: dict[str, Any]) -> None:
        """Process device status update notification.

        Extracts device ID and attributes from s2c_noti message and
        invokes update callback.

        Args:
            data: Parsed JSON message with cmd='s2c_noti'
        """
        device_data = data.get("data", {})
        device_id = device_data.get("did")
        attrs = device_data.get("attrs", {})

        if not device_id:
            _LOGGER.warning("Received device update without device ID")
            return

        if not attrs:
            _LOGGER.debug("Received empty attrs for device %s", device_id)
            return

        _LOGGER.debug("Device update: %s with %d attributes", device_id, len(attrs))

        # Invoke update callback
        try:
            self._update_callback(device_id, attrs)
        except Exception as ex:
            _LOGGER.error("Error in update callback: %s", ex)

    @property
    def is_connected(self) -> bool:
        """Return True if WebSocket is connected and authenticated.

        Returns:
            True if connection is active and login successful
        """
        return self._connected and self._authenticated
