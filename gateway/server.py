#!/usr/bin/env python3
"""
Indoor gateway - collects sensor data and POSTs to Pi5 dashboard.

Receives sensor data via LoRa from outdoor nodes, optionally reads local sensors,
and POSTs all data to the Pi5 dashboard's /api/timeseries/ingest endpoint.

Configuration is loaded from config/gateway_config.json:
{
    "node_id": "indoor-gateway",
    "dashboard_url": "http://192.168.1.100:5000",
    "local_sensors": [
        {"class": "BME280TempPressureHumidity"}
    ],
    "local_sensor_interval_sec": 5,
    "lora": {
        "enabled": true,
        "frequency_mhz": 915.0,
        "cs_pin": 24,
        "reset_pin": 25
    }
}

Usage:
    python3 -m gateway.server [config_file]
    python3 gateway/server.py [config_file]
"""

import argparse
import json
import logging
import inspect
import queue
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from gpiozero import Button
from utils.led import RgbLed
from utils.gateway_state import GatewayState
from utils.radio_state import RadioState
from gateway.http_handler import CommandServer
from display import (
    GatewayLocalSensors,
    LastPacketPage,
    OffPage,
    ScreenManager,
    SSD1306Display,
    SystemInfoPage,
)

import sensors as sensors_module
from radio import RFM9xRadio
from sensors import Sensor
from utils.protocol import (
    SensorReading,
    build_command_packet,
    make_sensor_id,
    parse_ack_packet,
    parse_lora_packet,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
cmd_logger = logging.getLogger("cmd_debug")


# =============================================================================
# Command Queue with ACK-based Reliability
# =============================================================================


@dataclass
class PendingCommand:
    """A command awaiting ACK from the target node."""

    command_id: str
    cmd: str
    args: list[str]
    node_id: str
    packet: bytes
    next_retry_time: float
    retry_count: int = 0
    max_retries: int = 10
    first_sent_time: float = 0.0


@dataclass
class DiscoveryRequest:
    """Request for node discovery, coordinated between HTTP and transceiver threads."""

    retries: int
    initial_retry_ms: int
    max_retry_ms: int
    retry_multiplier: float
    done: threading.Event
    nodes: list[str] = field(default_factory=list)
    error: str | None = None


class CommandQueue:
    """
    Serial command queue with ACK-based retirement.

    Commands are sent one at a time. After sending, the gateway waits for
    an ACK from the target node. If no ACK is received, the command is
    retried with multiplicative backoff until max_retries is reached.
    """

    def __init__(
        self,
        max_size: int = 128,
        max_retries: int = 10,
        initial_retry_ms: int = 500,
        max_retry_ms: int = 5000,
        retry_multiplier: float = 1.5,
        discovery_retries: int = 30,
    ):
        """
        Initialize the command queue.

        Args:
            max_size: Maximum number of pending commands
            max_retries: Maximum retry attempts before giving up
            initial_retry_ms: Initial retry delay in milliseconds
            max_retry_ms: Maximum retry delay (backoff cap)
            retry_multiplier: Backoff multiplier per retry (default 1.5)
            discovery_retries: Retry count for discovery operations
        """
        self._queue: deque[PendingCommand] = deque()
        self._max_size = max_size
        self._current: PendingCommand | None = None
        self._lock = threading.Lock()
        self._max_retries = max_retries
        self._initial_retry_ms = initial_retry_ms
        self._max_retry_ms = max_retry_ms
        self._retry_multiplier = retry_multiplier
        self._discovery_retries = discovery_retries
        self._completed_responses: dict[str, tuple[float, dict]] = {}
        self._response_ttl = 60.0  # seconds to keep completed responses

    # ─── Runtime Parameter Properties ───────────────────────────────────────

    @property
    def max_size(self) -> int:
        return self._max_size

    @max_size.setter
    def max_size(self, val: int) -> None:
        self._max_size = val

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @max_retries.setter
    def max_retries(self, val: int) -> None:
        self._max_retries = val

    @property
    def initial_retry_ms(self) -> int:
        return self._initial_retry_ms

    @initial_retry_ms.setter
    def initial_retry_ms(self, val: int) -> None:
        self._initial_retry_ms = val

    @property
    def max_retry_ms(self) -> int:
        return self._max_retry_ms

    @max_retry_ms.setter
    def max_retry_ms(self, val: int) -> None:
        self._max_retry_ms = val

    @property
    def retry_multiplier(self) -> float:
        return self._retry_multiplier

    @retry_multiplier.setter
    def retry_multiplier(self, val: float) -> None:
        self._retry_multiplier = val

    @property
    def discovery_retries(self) -> int:
        return self._discovery_retries

    @discovery_retries.setter
    def discovery_retries(self, val: int) -> None:
        self._discovery_retries = val

    def add(
        self, cmd: str, args: list[str], node_id: str, max_retries: int | None = None
    ) -> str | None:
        """
        Add a command to the queue.

        Args:
            cmd: Command name
            args: Command arguments
            node_id: Target node ID (empty for broadcast)
            max_retries: Override default retry count (for fire-and-forget commands)

        Returns:
            Command ID for tracking, or None if queue is full
        """
        packet, command_id = build_command_packet(cmd, args, node_id)
        retries = max_retries if max_retries is not None else self._max_retries

        pending = PendingCommand(
            command_id=command_id,
            cmd=cmd,
            args=args,
            node_id=node_id,
            packet=packet,
            next_retry_time=0,  # Send immediately
            max_retries=retries,
        )

        with self._lock:
            if len(self._queue) >= self._max_size:
                return None
            self._queue.append(pending)
        cmd_logger.debug(
            "CMD_QUEUED cmd=%s target=%s id=%s",
            cmd, node_id or "broadcast", command_id,
        )
        return command_id

    def get_next_to_send(self) -> PendingCommand | None:
        """
        Get the next command to transmit, if ready.

        Returns:
            PendingCommand if one is ready to send, None otherwise
        """
        with self._lock:
            # If no current command, pop from queue
            if self._current is None and self._queue:
                self._current = self._queue.popleft()
                self._current.next_retry_time = 0  # Send immediately

            # Check if current command is ready to send (retry timer elapsed)
            if self._current and time.time() >= self._current.next_retry_time:
                return self._current
        return None

    def mark_sent(self) -> None:
        """Mark the current command as sent and schedule retry."""
        with self._lock:
            if self._current:
                self._current.retry_count += 1
                if self._current.retry_count == 1:
                    self._current.first_sent_time = time.time()
                # Exponential backoff with configurable multiplier, capped
                delay_ms = min(
                    self._initial_retry_ms
                    * (self._retry_multiplier ** (self._current.retry_count - 1)),
                    self._max_retry_ms,
                )
                self._current.next_retry_time = time.time() + (delay_ms / 1000)
                cmd_logger.debug(
                    "CMD_RETRY cmd=%s attempt=%d next_in=%dms",
                    self._current.cmd, self._current.retry_count, int(delay_ms),
                )

    def ack_received(self, command_id: str, payload: dict | None = None) -> PendingCommand | None:
        """
        Handle an ACK - retire the command if it matches.

        Args:
            command_id: ID from the ACK packet
            payload: Optional response payload from node

        Returns:
            The retired PendingCommand if matched, None otherwise
        """
        with self._lock:
            if self._current and self._current.command_id == command_id:
                retired = self._current
                logger.info(
                    f"Command '{retired.cmd}' ACK'd after "
                    f"{retired.retry_count} attempt(s)"
                )
                # Store response for retrieval (empty dict for no-payload ACKs)
                self._completed_responses[command_id] = (
                    time.time(), payload if payload is not None else {}
                )
                self._current = None
                return retired
        return None

    def check_expired(self) -> PendingCommand | None:
        """
        Check if the current command has exceeded max retries.

        Returns:
            The expired PendingCommand if one expired, None otherwise
        """
        with self._lock:
            if self._current and self._current.retry_count >= self._current.max_retries:
                expired = self._current
                self._current = None
                return expired
        return None

    def pending_count(self) -> int:
        """Return number of commands in queue (not including current)."""
        with self._lock:
            return len(self._queue)

    def has_current(self) -> bool:
        """Return True if there's a command currently being sent/retried."""
        with self._lock:
            return self._current is not None

    def cancel(self, command_id: str) -> bool:
        """
        Cancel a pending command, removing it from current or queue.

        Used by wait-mode handlers to prevent a timed-out command from
        blocking subsequent commands in the serial queue.

        Args:
            command_id: ID of the command to cancel

        Returns:
            True if the command was found and cancelled
        """
        with self._lock:
            if self._current and self._current.command_id == command_id:
                logger.info(f"Cancelled current command {command_id}")
                self._current = None
                return True
            original_len = len(self._queue)
            self._queue = deque(
                p for p in self._queue if p.command_id != command_id
            )
            if len(self._queue) < original_len:
                logger.info(f"Cancelled queued command {command_id}")
                return True
        return False

    def wait_for_response(self, command_id: str, timeout: float = 10.0) -> dict | None:
        """
        Wait for a command to complete and return its response payload.

        Args:
            command_id: ID of the command to wait for
            timeout: Maximum seconds to wait

        Returns:
            Response payload dict, or None if timeout/no payload
        """
        logger.info(f"Waiting for response to {command_id} (timeout={timeout}s)")
        deadline = time.time() + timeout
        poll_count = 0
        while time.time() < deadline:
            with self._lock:
                # Check if response is available
                if command_id in self._completed_responses:
                    _, payload = self._completed_responses.pop(command_id)
                    logger.info(f"Got response for {command_id}: {payload}")
                    return payload
                # Check if command completed without payload
                is_current = self._current and self._current.command_id == command_id
                in_queue = any(p.command_id == command_id for p in self._queue)
                if not is_current and not in_queue:
                    # Command completed but no response stored
                    logger.info(
                        f"Command {command_id} completed without response "
                        f"after {poll_count} polls ({time.time() - (deadline - timeout):.1f}s)"
                    )
                    return None
            poll_count += 1
            time.sleep(0.1)
        logger.warning(f"Timeout waiting for {command_id} after {poll_count} polls")
        return None

    def cleanup_old_responses(self) -> None:
        """Remove expired response payloads."""
        now = time.time()
        with self._lock:
            expired = [
                cid for cid, (ts, _) in self._completed_responses.items()
                if now - ts > self._response_ttl
            ]
            for cid in expired:
                del self._completed_responses[cid]


# =============================================================================
# Dashboard Client
# =============================================================================


@dataclass
class PendingPost:
    """A batch of readings waiting to be posted to the dashboard."""

    datapoints: list[dict]
    node_id: str


class DashboardClient:
    """HTTP client for posting sensor data to the Pi5 dashboard."""

    def __init__(self, base_url: str, gateway_id: str, timeout: float = 10.0):
        """
        Initialize the dashboard client.

        Args:
            base_url: Dashboard URL (e.g., "http://192.168.1.100:5000")
            gateway_id: Gateway identifier to include with all data
            timeout: HTTP request timeout in seconds
        """
        self._base_url = base_url.rstrip('/')
        self._gateway_id = gateway_id
        self._timeout = timeout
        self._ingest_url = f"{self._base_url}/api/timeseries/ingest"

    def post_readings(self, readings: list[dict]) -> bool:
        """
        POST sensor readings to the dashboard.

        Args:
            readings: List of reading dicts with id, name, units, value, timestamp

        Returns:
            True if successful, False otherwise
        """
        if not readings:
            return True

        payload = {
            "gateway": self._gateway_id,
            "datapoints": readings
        }

        try:
            data = json.dumps(payload).encode('utf-8')
            req = Request(
                self._ingest_url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urlopen(req, timeout=self._timeout) as response:
                result = json.loads(response.read().decode('utf-8'))
                if result.get('success'):
                    logger.debug(f"Posted {result.get('count', len(readings))} readings")
                    return True
                else:
                    logger.error(f"Dashboard rejected data: {result.get('error')}")
                    return False
        except HTTPError as e:
            logger.error(f"HTTP error posting to dashboard: {e.code} {e.reason}")
            return False
        except URLError as e:
            logger.error(f"Connection error posting to dashboard: {e.reason}")
            return False
        except Exception as e:
            logger.error(f"Error posting to dashboard: {e}")
            return False


# =============================================================================
# Sensor Data Collector
# =============================================================================


class SensorDataCollector:
    """
    Collects sensor readings from LoRa and local sources, posts to dashboard.

    Uses a background thread to POST readings asynchronously, preventing
    HTTP latency from blocking the LoRa transceiver thread.
    """

    def __init__(
        self,
        gateway_id: str,
        dashboard_client: DashboardClient,
        max_queue_size: int = 100,
    ):
        """
        Initialize the collector with async posting.

        Args:
            gateway_id: Gateway identifier
            dashboard_client: Client for posting to dashboard
            max_queue_size: Maximum pending posts before dropping oldest
        """
        self._gateway_id = gateway_id
        self._dashboard_client = dashboard_client
        self._max_queue_size = max_queue_size
        self._post_queue: queue.Queue[PendingPost | None] = queue.Queue(
            maxsize=max_queue_size
        )
        self._running = False
        self._poster_thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background poster thread."""
        if self._running:
            return
        self._running = True
        self._poster_thread = threading.Thread(
            target=self._poster_loop, daemon=True, name="DashboardPoster"
        )
        self._poster_thread.start()
        logger.info("Dashboard poster thread started")

    def stop(self) -> None:
        """Stop the background poster thread gracefully."""
        if not self._running:
            return
        self._running = False
        # Send sentinel to unblock the queue
        try:
            self._post_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._poster_thread and self._poster_thread.is_alive():
            self._poster_thread.join(timeout=2.0)
        logger.info("Dashboard poster thread stopped")

    def _poster_loop(self) -> None:
        """Background loop that posts readings to the dashboard."""
        while self._running:
            try:
                pending = self._post_queue.get(timeout=1.0)
                if pending is None:
                    # Sentinel received, exit
                    break
                self._do_post(pending)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Dashboard poster error: {e}")

    def _do_post(self, pending: PendingPost) -> None:
        """Actually post readings to dashboard (runs in poster thread)."""
        success = self._dashboard_client.post_readings(pending.datapoints)
        if success:
            logger.info(f"Posted {len(pending.datapoints)} readings from '{pending.node_id}'")
        else:
            logger.warning(f"Failed to post {len(pending.datapoints)} readings from '{pending.node_id}'")

    def add_readings(
        self, node_id: str, readings: list[SensorReading], is_local: bool = False
    ) -> None:
        """
        Queue sensor readings for async posting to dashboard.

        This method returns immediately - actual posting happens in background.
        If the queue is full, the oldest pending post is dropped.

        Args:
            node_id: ID of the node that produced the readings
            readings: List of SensorReading objects
            is_local: True if readings are from local sensors (vs LoRa)
        """
        datapoints = []

        for reading in readings:
            sensor_id = make_sensor_id(node_id, reading.sensor_class, reading.name)

            # Build display name: "NodeId SensorClass ReadingName"
            # e.g., "Patio BME280 Temperature"
            display_name = f"{node_id} {reading.name}"

            datapoints.append({
                "id": sensor_id,
                "name": display_name,
                "units": reading.units,
                "value": reading.value,
                "timestamp": reading.timestamp,
                "category": "Local Sensors" if is_local else "Remote Sensors",
                "tags": [node_id, reading.sensor_class.lower(), "local" if is_local else "lora"],
            })

        if not datapoints:
            return

        pending = PendingPost(datapoints=datapoints, node_id=node_id)

        # Try to enqueue; if full, drop oldest and retry
        try:
            self._post_queue.put_nowait(pending)
        except queue.Full:
            try:
                # Drop oldest
                dropped = self._post_queue.get_nowait()
                if dropped:
                    logger.warning(
                        f"Dashboard queue full, dropped {len(dropped.datapoints)} "
                        f"readings from '{dropped.node_id}'"
                    )
                self._post_queue.put_nowait(pending)
            except queue.Empty:
                # Race condition - queue was drained, retry
                try:
                    self._post_queue.put_nowait(pending)
                except queue.Full:
                    logger.error("Dashboard queue still full after drop, losing readings")

    @property
    def gateway_id(self) -> str:
        return self._gateway_id


# =============================================================================
# LoRa Transceiver Thread
# =============================================================================


class LoRaTransceiver(threading.Thread):
    """
    Background thread that receives LoRa packets and sends commands.

    Handles both:
    - Receiving sensor data from nodes → forwards to collector
    - Receiving ACKs from nodes → retires commands from queue
    - Sending commands from queue → transmits over LoRa with retry
    """

    def __init__(
        self,
        radio: RFM9xRadio,
        collector: SensorDataCollector,
        command_queue: CommandQueue,
        led: RgbLed | None = None,
        flash_color: tuple[int, int, int] = (255, 0, 0),
        flash_duration: float = 0.1,
        gateway_state: GatewayState | None = None,
        verbose_logging: bool = False,
        n2g_freq: float = 915.0,
        g2n_freq: float = 915.5,
    ):
        super().__init__(daemon=True, name="LoRaTransceiver")
        self._radio = radio
        self._collector = collector
        self._command_queue = command_queue
        self._led = led
        self._flash_color = flash_color
        self._flash_duration = flash_duration
        self._flash_enabled = True  # Can be toggled via signals
        self._running = False
        self._gateway_state = gateway_state
        self._verbose_logging = verbose_logging
        self._n2g_freq = n2g_freq  # Node to Gateway: sensors + ACKs
        self._g2n_freq = g2n_freq  # Gateway to Node: commands
        self._discovery_request: DiscoveryRequest | None = None
        self._discovery_lock = threading.Lock()

    def request_discovery(self, request: DiscoveryRequest) -> bool:
        """Submit a discovery request. Returns False if one is already in progress."""
        with self._discovery_lock:
            if self._discovery_request is not None:
                return False
            self._discovery_request = request
            return True

    def set_flash_enabled(self, enabled: bool) -> None:
        """Enable or disable LED flash on receive."""
        self._flash_enabled = enabled
        logger.info(f"LED flash on receive: {'enabled' if enabled else 'disabled'}")

    def run(self) -> None:
        self._running = True
        logger.info("LoRa transceiver started")

        while self._running:
            try:
                # Apply any pending radio config changes (from HTTP handler)
                # Must be done here to avoid SPI contention with receive()
                if self._gateway_state and self._gateway_state.radio_state:
                    rs = self._gateway_state.radio_state
                    if rs.has_pending():
                        applied = rs.apply_pending()
                        if applied:
                            logger.info(
                                f"LoRaTransceiver applied config: {', '.join(applied)}"
                            )

                # Check for discovery request (takes priority)
                discovery = None
                with self._discovery_lock:
                    discovery = self._discovery_request

                if discovery is not None:
                    self._execute_discovery(discovery)
                    with self._discovery_lock:
                        self._discovery_request = None
                    continue

                # Receive with short timeout to allow command transmission
                rx_start = time.time()
                packet = self._radio.receive(timeout=0.1)
                rx_ms = (time.time() - rx_start) * 1000
                if packet is not None:
                    cmd_logger.debug(
                        "RX_PACKET len=%d after=%.0fms", len(packet), rx_ms
                    )
                    self._process_received_packet(packet)

                # Check for pending commands to transmit
                self._process_command_queue()

            except Exception as e:
                logger.error(f"LoRa transceiver error: {e}")
                time.sleep(1)  # Back off on error

    def stop(self) -> None:
        self._running = False

    def _process_command_queue(self) -> None:
        """Send pending commands with retry logic."""
        # Check for expired commands
        expired = self._command_queue.check_expired()
        if expired:
            target = expired.node_id or "broadcast"
            logger.warning(
                f"Command '{expired.cmd}' to {target} expired after "
                f"{expired.max_retries} retries"
            )
            cmd_logger.debug(
                "CMD_EXPIRED cmd=%s target=%s retries=%d",
                expired.cmd, target, expired.max_retries,
            )

        # Get next command to send (if retry timer elapsed)
        pending = self._command_queue.get_next_to_send()
        if pending:
            try:
                # Switch to G2N channel for command transmission
                tx_start = time.time()
                cmd_logger.debug("FREQ to=G2N freq=%.1fMHz", self._g2n_freq)
                self._radio.set_frequency(self._g2n_freq)
                success = self._radio.send(pending.packet)
                tx_ms = (time.time() - tx_start) * 1000
                # Switch back to N2G to receive ACK
                self._radio.set_frequency(self._n2g_freq)
                cmd_logger.debug(
                    "FREQ to=N2G freq=%.1fMHz (TX took %.0fms)",
                    self._n2g_freq, tx_ms,
                )

                target = pending.node_id or "broadcast"
                if success:
                    logger.debug(
                        f"Sent '{pending.cmd}' to {target} on G2N "
                        f"(attempt {pending.retry_count + 1})"
                    )
                    cmd_logger.debug(
                        "CMD_TX cmd=%s target=%s attempt=%d/%d bytes=%d tx_ms=%.0f",
                        pending.cmd, target, pending.retry_count + 1,
                        pending.max_retries, len(pending.packet), tx_ms,
                    )
                else:
                    logger.warning(f"Radio send failed for '{pending.cmd}' to {target}")
                self._command_queue.mark_sent()
                cmd_logger.debug(
                    "CMD_MARK_SENT next_retry_in=%.0fms",
                    (pending.next_retry_time - time.time()) * 1000,
                )
            except Exception as e:
                logger.error(f"Error sending command: {e}")
                # Ensure we're back on N2G even on error
                try:
                    self._radio.set_frequency(self._n2g_freq)
                except Exception:
                    pass

    def _execute_discovery(self, request: DiscoveryRequest) -> None:
        """
        Execute discovery loop: broadcast ping, collect ACKs, repeat with backoff.

        Runs inside the transceiver thread. Normal command processing is paused.
        Sensor data packets received during listen windows are still processed.
        """
        discovered_nodes: set[str] = set()
        logger.info(f"Starting node discovery ({request.retries} broadcasts)")

        try:
            delay_ms = float(request.initial_retry_ms)

            for attempt in range(request.retries):
                # Build and send broadcast discover on G2N
                packet, command_id = build_command_packet("discover", [], "")
                cmd_logger.debug("FREQ to=G2N freq=%.1fMHz", self._g2n_freq)
                self._radio.set_frequency(self._g2n_freq)
                success = self._radio.send(packet)
                self._radio.set_frequency(self._n2g_freq)
                cmd_logger.debug("FREQ to=N2G freq=%.1fMHz", self._n2g_freq)

                if not success:
                    logger.warning(f"Discovery broadcast {attempt + 1} send failed")

                logger.info(
                    f"Discovery broadcast {attempt + 1}/{request.retries} sent "
                    f"(listening for {delay_ms:.0f}ms)"
                )

                # Listen for ACKs during the backoff window
                listen_deadline = time.time() + (delay_ms / 1000.0)
                while time.time() < listen_deadline:
                    remaining = listen_deadline - time.time()
                    timeout = min(0.1, max(0.01, remaining))
                    raw = self._radio.receive(timeout=timeout)

                    if raw is None:
                        continue

                    # Try parsing as ACK
                    ack = parse_ack_packet(raw)
                    if ack:
                        # Forward to command queue in case it's for a queued command
                        self._command_queue.ack_received(
                            ack.command_id, payload=ack.payload
                        )
                        # Record the node for discovery
                        if ack.node_id not in discovered_nodes:
                            discovered_nodes.add(ack.node_id)
                            logger.info(
                                f"Discovery: found node '{ack.node_id}' "
                                f"(total: {len(discovered_nodes)})"
                            )
                        continue

                    # Not an ACK — process as sensor data
                    self._process_received_packet(raw)

                # Backoff for next iteration
                delay_ms = min(
                    delay_ms * request.retry_multiplier,
                    float(request.max_retry_ms),
                )

            request.nodes = sorted(discovered_nodes)
            logger.info(
                f"Discovery complete: {len(discovered_nodes)} node(s) found: "
                f"{request.nodes}"
            )

        except Exception as e:
            logger.error(f"Discovery error: {e}")
            request.error = str(e)
            # Ensure we return to N2G on error
            try:
                self._radio.set_frequency(self._n2g_freq)
            except Exception:
                pass

        finally:
            request.done.set()

    def _process_received_packet(self, packet: bytes) -> None:
        """Validate CRC, parse JSON, forward to collector or handle ACK."""
        receive_time = time.time()
        rssi = self._radio.get_last_rssi()

        # First, check if it's an ACK packet
        ack = parse_ack_packet(packet)
        if ack:
            retired = self._command_queue.ack_received(ack.command_id, payload=ack.payload)
            if retired:
                rtt_ms = (
                    (time.time() - retired.first_sent_time) * 1000
                    if retired.first_sent_time
                    else 0
                )
                logger.info(f"ACK received from '{ack.node_id}' (RSSI: {rssi} dB)")
                cmd_logger.debug(
                    "ACK_MATCH id=%s node=%s rssi=%s rtt_ms=%.0f attempts=%d payload=%s",
                    ack.command_id, ack.node_id, rssi, rtt_ms, retired.retry_count,
                    ack.payload,
                )
            else:
                # Log current command state for debugging
                with self._command_queue._lock:
                    current = self._command_queue._current
                    current_id = current.command_id if current else "none"
                logger.debug(f"Unexpected ACK from '{ack.node_id}': {ack.command_id}")
                cmd_logger.debug(
                    "ACK_STALE id=%s node=%s rssi=%s current_cmd=%s",
                    ack.command_id, ack.node_id, rssi, current_id,
                )
            return

        # Otherwise, process as sensor data
        result = parse_lora_packet(packet)
        if result is None:
            if self._verbose_logging:
                logger.warning(f"Invalid LoRa packet (RSSI: {rssi} dB): {packet!r}")
            else:
                logger.warning(f"Invalid LoRa packet (RSSI: {rssi} dB): {packet[:50]}...")
            return

        node_id, readings = result

        # Replace timestamp=0 with gateway receive time
        for reading in readings:
            if reading.timestamp == 0:
                reading.timestamp = receive_time
        logger.info(
            f"LoRa received from '{node_id}': {len(readings)} readings (RSSI: {rssi} dB)"
        )

        # Flash LED on successful receive
        if self._led and self._flash_enabled:
            self._led.flash(*self._flash_color, self._flash_duration)

        # Update gateway state with first reading for display
        if self._gateway_state and readings:
            r = readings[0]
            self._gateway_state.update_last_packet(
                node_id=node_id,
                rssi=rssi,
                sensor_name=r.name,
                sensor_value=r.value,
                sensor_units=r.units,
            )

        self._collector.add_readings(node_id, readings, is_local=False)


# =============================================================================
# Local Sensor Reader Thread
# =============================================================================


class LocalSensorReader(threading.Thread):
    """Background thread that reads local sensors and forwards to collector."""

    def __init__(
        self,
        node_id: str,
        sensors: list[tuple[Sensor, str]],
        collector: SensorDataCollector,
        interval_sec: float = 5.0,
        gateway_state: GatewayState | None = None,
    ):
        super().__init__(daemon=True)
        self._node_id = node_id
        self._sensors = sensors
        self._collector = collector
        self._interval_sec = interval_sec
        self._running = False
        self._gateway_state = gateway_state

    def run(self) -> None:
        self._running = True
        logger.info(
            f"Local sensor reader started ({len(self._sensors)} sensors, "
            f"{self._interval_sec}s interval)"
        )

        while self._running:
            try:
                readings = self._read_sensors()
                if readings:
                    self._collector.add_readings(
                        self._node_id, readings, is_local=True
                    )
                    # Update gateway state for display
                    if self._gateway_state:
                        self._gateway_state.update_local_sensors([
                            (r.name, r.value, r.units) for r in readings
                        ])
            except Exception as e:
                logger.error(f"Local sensor read error: {e}")

            time.sleep(self._interval_sec)

    def stop(self) -> None:
        self._running = False

    def _read_sensors(self) -> list[SensorReading]:
        """Read all local sensors and return readings."""
        readings = []
        timestamp = time.time()

        for sensor, class_name in self._sensors:
            try:
                values = sensor.read()
                names = sensor.get_names()
                units = sensor.get_units()

                for value, name, unit in zip(values, names, units):
                    readings.append(
                        SensorReading(
                            name=name,
                            units=unit,
                            value=value,
                            sensor_class=class_name,
                            timestamp=timestamp,
                        )
                    )
            except Exception as e:
                logger.error(f"Error reading local {class_name}: {e}")

        return readings


# =============================================================================
# Sensor Instantiation
# =============================================================================


def get_sensor_class(class_name: str) -> type[Sensor] | None:
    """Get a Sensor class by name using reflection."""
    for name, obj in inspect.getmembers(sensors_module, inspect.isclass):
        if name == class_name and issubclass(obj, Sensor) and obj is not Sensor:
            return obj
    return None


def instantiate_sensors(sensor_configs: list[dict]) -> list[tuple[Sensor, str]]:
    """Instantiate sensors from configuration."""
    sensors = []

    for config in sensor_configs:
        class_name = config.get("class")
        if not class_name:
            continue

        sensor_class = get_sensor_class(class_name)
        if sensor_class is None:
            logger.warning(f"Unknown sensor class: {class_name}")
            continue

        try:
            kwargs = config.get("config", {})
            sensor = sensor_class(**kwargs)
            sensor.init()
            sensors.append((sensor, class_name))
            logger.info(f"Initialized local sensor: {class_name}")
        except Exception as e:
            logger.error(f"Failed to initialize {class_name}: {e}")

    return sensors


# =============================================================================
# Main
# =============================================================================


def load_config(config_path: str) -> dict:
    """Load gateway configuration from JSON file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        return json.load(f)


def run_gateway(
    config: dict,
    config_path: str,
    verbose_logging: bool = False,
    cmd_debug: bool = False,
) -> None:
    """Run the gateway."""
    if verbose_logging:
        logger.info("Verbose logging enabled")

    if cmd_debug:
        # Focused CMD/ACK logger with millisecond timestamps
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d [CMD] %(message)s", datefmt="%H:%M:%S"
            )
        )
        cmd_logger.addHandler(handler)
        cmd_logger.setLevel(logging.DEBUG)
        # Suppress sensor/HTTP noise on the main logger
        logging.getLogger(__name__).setLevel(logging.WARNING)
        cmd_logger.debug("CMD debug mode active")

    node_id = config.get("node_id", "gateway")
    dashboard_url = config.get("dashboard_url")

    if not dashboard_url:
        logger.error("dashboard_url not configured")
        sys.exit(1)

    # Create shared state for gateway components
    gateway_state = GatewayState()
    gateway_state.node_id = node_id
    gateway_state.config_path = config_path
    gateway_state.dashboard_url = dashboard_url

    # Create dashboard client and collector
    dashboard_client = DashboardClient(dashboard_url, node_id)
    collector = SensorDataCollector(node_id, dashboard_client)
    collector.start()

    logger.info(f"Gateway '{node_id}' posting to {dashboard_url}")

    # Initialize LED if configured
    led = None
    led_config = config.get("led", {})
    if led_config:
        try:
            led = RgbLed(
                red_bcm=led_config.get("red_bcm", 17),
                green_bcm=led_config.get("green_bcm", 27),
                blue_bcm=led_config.get("blue_bcm", 22),
                common_anode=led_config.get("common_anode", True),
            )
            logger.info("LED initialized for flash-on-receive")
        except Exception as e:
            logger.warning(f"Failed to initialize LED: {e}")
            led = None

    flash_color = tuple(led_config.get("flash_color", [255, 0, 0]))
    flash_duration = led_config.get("flash_duration_sec", 0.1)
    flash_on_recv_default = led_config.get("flash_on_recv", True)

    # Create command queue for gateway → node commands with ACK-based reliability
    command_config = config.get("command_server", {})
    command_queue = CommandQueue(
        max_size=command_config.get("max_queue_size", 128),
        max_retries=command_config.get("max_retries", 10),
        initial_retry_ms=command_config.get("initial_retry_ms", 500),
        max_retry_ms=command_config.get("max_retry_ms", 5000),
        retry_multiplier=command_config.get("retry_multiplier", 1.5),
        discovery_retries=command_config.get("discovery_retries", 30),
    )
    gateway_state.command_queue = command_queue

    # Build discovery config from command_server section
    discovery_config = {
        "discovery_retries": command_config.get("discovery_retries", 30),
        "initial_retry_ms": command_config.get("initial_retry_ms", 500),
        "max_retry_ms": command_config.get("max_retry_ms", 5000),
        "retry_multiplier": command_config.get("retry_multiplier", 1.5),
    }

    # Start command server if enabled
    command_server = None

    if command_config.get("enabled", False):
        port = command_config.get("port", 5001)
        command_server = CommandServer(
            port=port,
            command_queue=command_queue,
            discovery_config=discovery_config,
        )
        command_server.start()
        logger.info(f"Command server listening on port {port}")

    # Start LoRa transceiver if enabled
    lora_transceiver = None
    radio = None
    lora_config = config.get("lora", {})

    if lora_config.get("enabled", True):
        try:
            # Dual-channel: N2G for sensors+ACKs, G2N for commands
            n2g_freq = lora_config.get("n2g_frequency_mhz", 915.0)
            g2n_freq = lora_config.get("g2n_frequency_mhz", 915.5)

            radio = RFM9xRadio(
                frequency_mhz=n2g_freq,  # Start on N2G (sensors + ACKs)
                tx_power=lora_config.get("tx_power", 23),
                cs_pin=lora_config.get("cs_pin", 24),
                reset_pin=lora_config.get("reset_pin", 25),
            )
            radio.init()

            # Create RadioState (shared class with nodes)
            radio_state = RadioState(
                radio=radio,
                n2g_freq=n2g_freq,
                g2n_freq=g2n_freq,
            )
            gateway_state.radio_state = radio_state

            lora_transceiver = LoRaTransceiver(
                radio,
                collector,
                command_queue=command_queue,
                led=led,
                flash_color=flash_color,
                flash_duration=flash_duration,
                gateway_state=gateway_state,
                verbose_logging=verbose_logging,
                n2g_freq=n2g_freq,
                g2n_freq=g2n_freq,
            )
            lora_transceiver.set_flash_enabled(flash_on_recv_default)
            lora_transceiver.start()

            # Log all radio parameters at startup
            sf = radio_state.spreading_factor
            bw_hz = radio_state.signal_bandwidth
            bw_khz = bw_hz // 1000
            txpwr = radio_state.tx_power
            logger.info(
                f"LoRa radio initialized: SF={sf}, BW={bw_khz}kHz, "
                f"TXpwr={txpwr}dBm, N2G={n2g_freq}MHz, G2N={g2n_freq}MHz"
            )
            # Wire transceiver and params to command server
            if command_server:
                command_server.set_transceiver(lora_transceiver)
                command_server.set_gateway_state(gateway_state)
        except Exception as e:
            logger.error(f"Failed to initialize LoRa: {e}")
            logger.info("Continuing without LoRa transceiver")

    # Start local sensor reader if configured
    local_reader = None
    local_sensor_configs = config.get("local_sensors", [])

    if local_sensor_configs:
        local_sensors = instantiate_sensors(local_sensor_configs)
        if local_sensors:
            interval = config.get("local_sensor_interval_sec", 5.0)
            local_reader = LocalSensorReader(
                node_id, local_sensors, collector, interval, gateway_state
            )
            local_reader.start()

    # Initialize OLED display if configured
    screen_manager = None
    display_advance_button = None
    display_scroll_button = None
    display_config = config.get("display", {})

    if display_config.get("enabled", False):
        try:
            display = SSD1306Display(
                i2c_port=display_config.get("i2c_port", 1),
                i2c_address=display_config.get("i2c_address", 0x3C),
            )
            pages = [
                OffPage(),
                SystemInfoPage(gateway_state),
                LastPacketPage(gateway_state),
                GatewayLocalSensors(gateway_state),
            ]
            screen_manager = ScreenManager(
                display=display,
                pages=pages,
                refresh_interval=display_config.get("refresh_interval", 0.5),
            )
            screen_manager.start()

            # Set up optional GPIO buttons for page cycling and scrolling
            if advance_pin := display_config.get("advance_switch_pin"):
                display_advance_button = Button(advance_pin, bounce_time=0.02)
                display_advance_button.when_pressed = screen_manager.advance_page
                logger.info(f"Display advance button on GPIO {advance_pin}")

            if scroll_pin := display_config.get("scroll_switch_pin"):
                display_scroll_button = Button(scroll_pin, bounce_time=0.02)
                display_scroll_button.when_pressed = lambda: screen_manager.scroll_page(1)
                logger.info(f"Display scroll button on GPIO {scroll_pin}")

            logger.info("OLED display initialized")
        except Exception as e:
            logger.warning(f"Failed to initialize display: {e}")
            screen_manager = None

    # Set up signal handlers for runtime LED/display toggle
    def enable_flash(signum, frame):
        logger.info("Received SIGUSR1 signal - enabling LED flash")
        if lora_transceiver:
            lora_transceiver.set_flash_enabled(True)

    def disable_flash(signum, frame):
        logger.info("Received SIGUSR2 signal - disabling LED flash and display")
        if lora_transceiver:
            lora_transceiver.set_flash_enabled(False)
        if screen_manager:
            screen_manager.set_page(0)  # Switch to OffPage

    signal.signal(signal.SIGUSR1, enable_flash)
    signal.signal(signal.SIGUSR2, disable_flash)
    logger.info("Signal handlers registered (SIGUSR1=enable, SIGUSR2=disable LED/display)")

    # Run forever (threads are daemon threads, so Ctrl+C will stop everything)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        # Cleanup
        logger.info("Shutting down...")
        if lora_transceiver:
            lora_transceiver.stop()
        if command_server:
            command_server.stop()
        if local_reader:
            local_reader.stop()
        collector.stop()
        if screen_manager:
            screen_manager.close()
        if display_advance_button:
            display_advance_button.close()
        if display_scroll_button:
            display_scroll_button.close()
        if radio:
            radio.close()
        if led:
            led.close()


def main():
    parser = argparse.ArgumentParser(
        description="Indoor gateway - posts sensor data to Pi5 dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="config/gateway_config.json",
        help="Path to config file (default: config/gateway_config.json)",
    )
    parser.add_argument(
        "--verbose_logging",
        action="store_true",
        help="Enable verbose logging (full error messages, no truncation)",
    )
    parser.add_argument(
        "--cmd-debug",
        action="store_true",
        help="CMD/ACK focused logging: suppress sensor data, show command lifecycle",
    )
    args = parser.parse_args()

    # Load configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    # Run gateway
    run_gateway(
        config,
        config_path=args.config,
        verbose_logging=args.verbose_logging,
        cmd_debug=args.cmd_debug,
    )


if __name__ == "__main__":
    main()
