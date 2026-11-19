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
from sensors import LoRaLinkSensor
from utils.gateway_state import GatewayState
from utils.led import RgbLed
from utils.protocol import build_command_packet, parse_ack_packet, parse_lora_packet

logger = logging.getLogger(__name__)
cmd_logger = logging.getLogger("cmd_debug")

# --- Radio health ------------------------------------------------------------
# The SX1276 can wedge (rx_done stuck high, FIFO returning stale bytes, or a
# brownout dropping it back to FSK defaults). Nodes already self-heal; the
# gateway is the one receiver every node depends on, so it must too.

# receive() returning None this fast means rx_done fired but the packet was
# dropped (CRC error). Occasional = weak signal; back-to-back = stuck receiver.
INSTANT_RX_S = 0.005
BAD_RX_RESET_COUNT = 10  # unreadable packets in a row -> hard reset (node uses 10)
BAD_TX_RESET_COUNT = 3  # failed transmissions in a row -> hard reset
SILENCE_RESET_S = 900.0  # no valid packet for this long -> hard reset ...
SILENCE_RESET_MAX_S = 4 * 3600.0  # ... backing off while it stays quiet
RECONNECT_INTERVAL_S = 3.0  # recovery mode: pause between rebuild attempts
RECONNECT_LOG_INTERVAL_S = 300.0  # recovery mode: "still trying" log cadence
INVALID_LOG_INTERVAL_S = 10.0


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
        link_sensors: list[LoRaLinkSensor] | None = None,
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
        # LoRaRSSI/LoRaSNR local sensors fed with every valid sensor packet
        self._link_sensors = list(link_sensors or [])
        self._discovery_request: DiscoveryRequest | None = None
        self._discovery_lock = threading.Lock()
        self._radio_disconnected = False
        self._disconnect_start: float = 0.0
        self._next_reconnect_log: float = 0.0

        # Health (read by the gateway's main-loop watchdog)
        # Refreshed every loop iteration, including while reconnecting: an
        # unplugged adapter is handled in process, not by a restart.
        self.heartbeat = time.monotonic()
        self.resets = 0
        self.reconnects = 0
        self.silence_reset_s = SILENCE_RESET_S  # 0 disables silence resets
        self._bad_rx = 0
        self._bad_tx = 0
        self._last_valid = time.monotonic()
        self._silence_window = SILENCE_RESET_S
        self._invalid_suppressed = 0
        self._invalid_log_next = 0.0

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
        self._last_valid = time.monotonic()
        self._silence_window = self.silence_reset_s
        logger.info("LoRa transceiver started")

        while self._running:
            self.heartbeat = time.monotonic()
            # Recovery branch: stop normal RX/TX and try to rebuild the radio.
            if self._radio_disconnected:
                self._try_reconnect()
                continue

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
                rx_start = time.monotonic()
                packet = self._radio.receive(timeout=0.1)
                rx_s = time.monotonic() - rx_start
                if packet is not None:
                    cmd_logger.debug(
                        "RX_PACKET len=%d after=%.0fms", len(packet), rx_s * 1000
                    )
                    valid = self._process_received_packet(packet)
                else:
                    valid = False if rx_s < INSTANT_RX_S else None
                self._note_rx(valid)

                # Check for pending commands to transmit
                self._process_command_queue()
                self._command_queue.maybe_cleanup()

            except Exception as e:
                # Any radio-touching exception is treated as a likely USB
                # disconnect.  Subsequent calls may silently return junk
                # (0xFF reads from a dead SPI), so we can't wait for a
                # clean failure pattern — we have to trip immediately.
                # Reconnect is cheap (~100ms) when the device is healthy.
                logger.warning(
                    "Radio error (%s). Entering recovery mode.", e
                )
                self._radio_disconnected = True
                self._disconnect_start = time.monotonic()
                # Schedule first "still trying" log 5 min after disconnect.
                self._next_reconnect_log = self._disconnect_start + RECONNECT_LOG_INTERVAL_S

    def stop(self, timeout: float = 3.0) -> None:
        self._running = False
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)

    # -------------------------------------------------------------------------
    # Radio health
    # -------------------------------------------------------------------------

    def _try_reconnect(self) -> None:
        """One recovery-mode attempt to rebuild the radio stack."""
        try:
            ok = self._radio.reconnect()
            if ok:
                # The cached frequency may be G2N if the radio died mid-command
                # (the switch back failed), and reconnect() restores the cache.
                self._radio.set_frequency(self._n2g_freq)
        except Exception as e:  # reconnect() shouldn't raise; this thread must not die
            logger.debug("Radio reconnect attempt raised: %s", e)
            ok = False

        now = time.monotonic()
        if ok:
            logger.info("Radio reconnected after %.1fs", now - self._disconnect_start)
            self._radio_disconnected = False
            self.reconnects += 1
            # Fresh radio: restart the receive-health bookkeeping
            self._bad_rx = 0
            self._bad_tx = 0
            self._last_valid = now
            self._silence_window = self.silence_reset_s
            return

        if now >= self._next_reconnect_log:
            logger.info(
                "Still trying to reconnect to radio (%.1f min elapsed)",
                (now - self._disconnect_start) / 60.0,
            )
            self._next_reconnect_log = now + RECONNECT_LOG_INTERVAL_S
        time.sleep(RECONNECT_INTERVAL_S)

    def _note_rx(self, valid: bool | None) -> None:
        """Track receive health. valid: True = good packet, False = unreadable
        (garbage, or dropped on CRC), None = nothing heard (normal timeout)."""
        now = time.monotonic()
        if valid:
            self._bad_rx = 0
            self._last_valid = now
            self._silence_window = self.silence_reset_s
        elif valid is False:
            self._bad_rx += 1
            if self._bad_rx >= BAD_RX_RESET_COUNT:
                self._reset_radio(f"{self._bad_rx} unreadable packets in a row")
                self._bad_rx = 0
        if self.silence_reset_s and now - self._last_valid >= self._silence_window:
            # Nodes report every few minutes at most; a long silence usually
            # means the receiver is wedged. If it's genuinely quiet, back off.
            self._reset_radio(f"no valid packet for {self._silence_window / 60:.0f} min")
            self._last_valid = now
            self._silence_window = min(self._silence_window * 2, SILENCE_RESET_MAX_S)

    def _reset_radio(self, reason: str) -> None:
        logger.warning(f"Resetting LoRa radio: {reason}")
        try:
            self._radio.hard_reset()
            self.resets += 1
        except Exception as e:
            logger.error(f"LoRa radio reset failed: {e}")

    def _log_invalid(self, message: str) -> None:
        """Rate-limited warning: a wedged radio can produce garbage hundreds
        of times a second, which would bury useful logs and wear the SD card."""
        self._invalid_suppressed += 1
        now = time.monotonic()
        if now >= self._invalid_log_next:
            extra = self._invalid_suppressed - 1
            suffix = f" (+{extra} more in the last {INVALID_LOG_INTERVAL_S:.0f}s)" if extra else ""
            logger.warning(message + suffix)
            self._invalid_suppressed = 0
            self._invalid_log_next = now + INVALID_LOG_INTERVAL_S

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
                tx_start = time.monotonic()
                cmd_logger.debug("FREQ to=G2N freq=%.1fMHz", self._g2n_freq)
                self._radio.set_frequency(self._g2n_freq)
                success = self._radio.send(pending.packet)
                tx_ms = (time.monotonic() - tx_start) * 1000
                # Switch back to N2G to receive ACK
                self._radio.set_frequency(self._n2g_freq)
                cmd_logger.debug(
                    "FREQ to=N2G freq=%.1fMHz (TX took %.0fms)",
                    self._n2g_freq, tx_ms,
                )

                target = pending.node_id or "broadcast"
                if success:
                    self._bad_tx = 0
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
                    self._bad_tx += 1
                    if self._bad_tx >= BAD_TX_RESET_COUNT:
                        self._reset_radio(f"{self._bad_tx} failed transmissions in a row")
                        self._bad_tx = 0
                self._command_queue.mark_sent()
                cmd_logger.debug(
                    "CMD_MARK_SENT next_retry_in=%.0fms",
                    (pending.next_retry_time - time.monotonic()) * 1000,
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
                listen_deadline = time.monotonic() + (delay_ms / 1000.0)
                while time.monotonic() < listen_deadline:
                    self.heartbeat = time.monotonic()
                    remaining = listen_deadline - time.monotonic()
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

    def _process_received_packet(self, packet: bytes) -> bool:
        """Validate CRC, parse JSON, forward to collector or handle ACK.

        Returns True for a valid packet (ACK or sensor data), False otherwise.
        """
        receive_time = time.time()
        rssi = self._radio.get_last_rssi()
        snr = self._radio.get_last_snr()

        # First, check if it's an ACK packet
        ack = parse_ack_packet(packet)
        if ack:
            retired = self._command_queue.ack_received(
                ack.command_id, node_id=ack.node_id, payload=ack.payload
            )
            if retired:
                rtt_ms = (
                    (time.monotonic() - retired.first_sent_time) * 1000
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
            return True

        # Otherwise, process as sensor data
        result = parse_lora_packet(packet)
        if result is None:
            # Log hex dump for debugging packet issues
            hex_bytes = ' '.join(f'{b:02x}' for b in packet[:80])
            self._log_invalid(
                f"Invalid LoRa packet (RSSI: {rssi} dB, len={len(packet)}): {hex_bytes}"
            )
            return False

        node_id, readings = result

        # Replace timestamp=0 with gateway receive time
        for reading in readings:
            if reading.timestamp == 0:
                reading.timestamp = receive_time
        logger.info(
            f"LoRa received from '{node_id}': {len(readings)} readings "
            f"(RSSI: {rssi} dB, SNR: {snr} dB)"
        )

        # Feed link-quality local sensors (they ignore nodes they don't log)
        for link_sensor in self._link_sensors:
            try:
                link_sensor.record_packet(node_id, rssi, snr, receive_time)
            except Exception as e:
                logger.error(f"Link sensor {type(link_sensor).__name__} error: {e}")

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

        self._collector.add_readings(node_id, readings, is_local=False, rssi=rssi)
        return True
