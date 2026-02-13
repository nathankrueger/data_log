"""
LoRa transceiver thread for gateway radio communication.

Classes:
    LoRaTransceiver: Background thread for receiving LoRa packets and sending commands
"""

import logging
import threading
import time

from gateway.command_queue import CommandQueue, DiscoveryRequest
from gateway.sensor_collection import SensorDataCollector
from radio import RFM9xRadio
from utils.gateway_state import GatewayState
from utils.led import RgbLed
from utils.protocol import build_command_packet, parse_ack_packet, parse_lora_packet

logger = logging.getLogger(__name__)
cmd_logger = logging.getLogger("cmd_debug")


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
                            ack.command_id, node_id=ack.node_id, payload=ack.payload
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
            retired = self._command_queue.ack_received(
                ack.command_id, node_id=ack.node_id, payload=ack.payload
            )
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
            # Log hex dump for debugging packet issues
            hex_bytes = ' '.join(f'{b:02x}' for b in packet[:80])
            logger.warning(
                f"Invalid LoRa packet (RSSI: {rssi} dB, len={len(packet)}): {hex_bytes}"
            )
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
