#!/usr/bin/env python3
"""
Outdoor sensor node broadcaster.

Reads configured sensors and broadcasts readings via LoRa at configurable
per-sensor intervals. Designed to run on a Pi Zero 2W with sensors and
LoRa radio attached.

Configuration is loaded from config/node_config.json:
{
    "node_id": "patio",
    "sensors": [
        {"class": "BME280TempPressureHumidity", "interval_sec": 60},
        {"class": "MMA8452Accelerometer", "interval_sec": 1}
    ],
    "default_sensor_interval_sec": 30,
    "lora": {
        "n2g_frequency_hz": 915000000,
        "g2n_frequency_hz": 915500000,
        "spreading_factor": 7,
        "bandwidth": 0,
        "tx_power": 23
    }
}

Radio parameters (sf, bw, txpwr, n2gfreq, g2nfreq) can be changed at runtime
via setparam command and persisted with savecfg.

Each sensor can have its own "interval_sec". If not specified, falls back
to the global "default_sensor_interval_sec" (default: 30s).

Usage:
    python3 -m node.data_log [config_file]
    python3 node/data_log.py [config_file]
"""

import argparse
import inspect
import json
import logging
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import sensors as sensors_module
from radio import RFM9xRadio
from sensors import Sensor
from node.command import commands_init
from utils.command_registry import CommandRegistry
from utils.protocol import (
    SensorReading,
    build_ack_packet,
    build_lora_packets,
    parse_command_packet,
)
from utils.node_state import NodeState
from utils.radio_state import RadioState

# Hardware pin constants (RFM9x radio on Pi Zero 2W)
LORA_CS_PIN = 24
LORA_RESET_PIN = 25

# Bandwidth encoding: matches AB01 convention (0/1/2 -> Hz)
BW_HZ_MAP = {0: 125000, 1: 250000, 2: 500000}

# Shutdown flag for graceful SIGTERM handling
_shutdown_requested = False


def _signal_handler(signum: int, frame) -> None:
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global _shutdown_requested
    _shutdown_requested = True
    logger.info(f"Received signal {signum}, requesting shutdown...")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@dataclass
class SensorEntry:
    """A sensor instance with its broadcast configuration."""

    sensor: Sensor
    interval_sec: float
    last_broadcast: float = 0.0

    @property
    def class_name(self) -> str:
        """Get the sensor's class name."""
        return type(self.sensor).__name__




# =============================================================================
# Command Receiver Thread
# =============================================================================


class CommandReceiver(threading.Thread):
    """
    Dedicated thread for receiving commands immediately.

    Runs continuously, acquiring radio_lock for short receive windows.
    This ensures commands are received promptly even while the main
    broadcast loop is sleeping between broadcasts.

    Reads frequencies dynamically from RadioState to see updates from rcfg_radio.
    """

    def __init__(
        self,
        radio: RFM9xRadio,
        radio_lock: threading.Lock,
        node_id: str,
        registry: CommandRegistry,
        receive_timeout: float = 0.5,
        radio_state: RadioState | None = None,
        broadcast_ack_jitter_sec: float = 0.5,
    ):
        """
        Initialize the command receiver.

        Args:
            radio: Radio instance to receive from
            radio_lock: Lock shared with broadcast loop for half-duplex coordination
            node_id: This node's ID for command filtering
            registry: Command registry for dispatching received commands
            receive_timeout: Timeout for each receive attempt (default 0.5s)
            radio_state: RadioState for dynamic frequency reading (sees rcfg_radio updates)
            broadcast_ack_jitter_sec: Max random delay before ACKing broadcast commands
        """
        super().__init__(daemon=True, name="CommandReceiver")
        self._radio = radio
        self._radio_lock = radio_lock
        self._node_id = node_id
        self._registry = registry
        self._receive_timeout = receive_timeout
        self._radio_state = radio_state
        self._broadcast_ack_jitter_sec = broadcast_ack_jitter_sec
        self._running = False
        # Single-slot dedup (matches AB01 pattern)
        self._last_command_id: str = ""
        self._last_ack_packet: bytes | None = None

    def _get_n2g_freq(self) -> float:
        """Get current N2G frequency (from RadioState if available)."""
        if self._radio_state:
            return self._radio_state.n2g_freq
        return 915.0  # Fallback

    def _get_g2n_freq(self) -> float:
        """Get current G2N frequency (from RadioState if available)."""
        if self._radio_state:
            return self._radio_state.g2n_freq
        return 915.5  # Fallback

    def run(self) -> None:
        """Run the receive loop."""
        self._running = True
        logger.info(
            f"Command receiver started (G2N={self._get_g2n_freq()} MHz, "
            f"N2G={self._get_n2g_freq()} MHz)"
        )

        while self._running:
            try:
                # Use interruptible receive with fine-grained internal locking
                # This allows broadcast_loop to transmit during 100ms sleep intervals
                # while maintaining long effective RX windows (4+ seconds)
                packet = self._receive_interruptible(self._receive_timeout)

                if packet is not None:
                    self._process_packet(packet)

            except Exception as e:
                logger.error(f"Command receive error: {e}")
                time.sleep(0.5)  # Back off on error

    def stop(self) -> None:
        """Signal the thread to stop."""
        self._running = False

    def _send_ack(self, ack_packet: bytes, add_jitter: bool = False) -> bool:
        """Send an ACK packet, optionally applying jitter to stagger responses.

        Holds the lock during jitter to prevent broadcast_loop from transmitting
        (which would block command reception). Matches AB01 pattern where jitter
        and ACK send happen atomically within sendAckAndResumeRx().

        Explicitly sets frequency to N2G before sending (defensive, matches AB01).
        This ensures ACK goes out on correct frequency regardless of any race.
        """
        n2g_freq = self._get_n2g_freq()
        g2n_freq = self._get_g2n_freq()
        t0 = time.time()
        with self._radio_lock:
            lock_wait_ms = (time.time() - t0) * 1000
            if lock_wait_ms > 10:
                logger.warning(f"ACK lock wait: {lock_wait_ms:.0f}ms")

            # Jitter INSIDE lock to prevent broadcast_loop from grabbing radio
            if add_jitter and self._broadcast_ack_jitter_sec > 0:
                jitter = random.uniform(0, self._broadcast_ack_jitter_sec)
                logger.debug(f"Broadcast ACK jitter: {jitter * 1000:.0f}ms")
                time.sleep(jitter)

            # Set N2G frequency, send ACK, then switch back to G2N (matches AB01)
            self._radio.set_frequency(n2g_freq)
            success = self._radio.send(ack_packet)
            self._radio.set_frequency(g2n_freq)  # Resume on G2N for next receive
            logger.info(f"ACK sent on N2G={n2g_freq} MHz, success={success}")
            return success

    def _receive_interruptible(self, timeout: float) -> bytes | None:
        """Receive with interruptible polling and fine-grained locking.

        Uses fine-grained locking to allow broadcast_loop to transmit during
        sleep intervals while maintaining long effective RX windows:
        1. Acquires lock briefly each iteration (~10ms)
        2. Sets frequency and enters RX mode (handles rcfg_radio changes)
        3. Checks rx_done() and reads packet if available
        4. Releases lock during 100ms sleep (broadcast_loop can transmit)

        This gives us AB01-like continuous RX windows (4+ seconds total) while:
        - Responding to shutdown within ~100ms
        - Not blocking broadcast_loop for entire timeout duration
        - Handling frequency changes from rcfg_radio

        Args:
            timeout: Maximum time to wait for a packet (seconds)

        Returns:
            Received packet bytes, or None if timeout/shutdown
        """
        start = time.monotonic()

        while self._running:
            with self._radio_lock:
                # Set frequency and enter RX mode each iteration
                # (broadcast_loop may have changed frequency, or rcfg_radio applied)
                self._radio.set_frequency(self._get_g2n_freq())
                self._radio.listen()

                # Check for packet
                if self._radio.rx_done():
                    # Read packet immediately while we have lock
                    # Note: timeout=0 doesn't work (library times out before reading)
                    return self._radio.receive(timeout=0.5)

            # Release lock during sleep - broadcast_loop can transmit
            if time.monotonic() - start >= timeout:
                return None  # Timeout

            time.sleep(0.1)  # 100ms between polls - fast shutdown response

        return None  # Shutdown requested

    def _process_packet(self, packet: bytes) -> None:
        """Parse and dispatch a received command packet, send ACK.

        Follows the AB01 earlyAck pattern:
        - early_ack=True: Send ACK before dispatching handler
        - early_ack=False: Dispatch handler first, send ACK after with response payload

        Dedup: If the same command_id is received again (retransmission),
        resend the cached ACK but skip handler re-execution (matches AB01).
        """
        cmd = parse_command_packet(packet)
        if cmd is None:
            # Not a valid command packet (might be a sensor packet from another node)
            return

        # Check if command is for this node
        if cmd.node_id and cmd.node_id != self._node_id:
            return  # Not for us (targeted to another node)

        target = cmd.node_id if cmd.node_id else "broadcast"
        command_id = cmd.get_command_id()
        is_duplicate = command_id == self._last_command_id

        # Look up handler to check early_ack flag
        handler = self._registry.lookup(cmd.command, cmd.node_id)
        use_early_ack = handler is None or handler.early_ack
        # Add jitter for ALL broadcast responses to prevent ACK collisions
        is_broadcast = cmd.node_id == ""
        add_jitter = is_broadcast and self._broadcast_ack_jitter_sec > 0

        if is_duplicate:
            logger.info(
                f"Duplicate command '{cmd.command}' for {target} "
                f"(id: {command_id}), resending cached ACK"
            )
            if self._last_ack_packet is not None:
                self._send_ack(self._last_ack_packet, add_jitter)
            return

        logger.info(f"Received command '{cmd.command}' for {target} (id: {command_id})")

        # For early_ack handlers, send ACK before dispatch
        if use_early_ack:
            ack_packet = build_ack_packet(command_id, self._node_id)
            self._last_command_id = command_id
            self._last_ack_packet = ack_packet
            success = self._send_ack(ack_packet, add_jitter)
            if success:
                logger.debug(f"Sent early ACK for '{cmd.command}' (id: {command_id})")
            else:
                logger.warning(f"Failed to send early ACK for '{cmd.command}'")

        # Dispatch to handlers
        handled, response = self._registry.dispatch(
            cmd.command, cmd.args, cmd.node_id
        )
        if not handled:
            logger.debug(f"No handler for command '{cmd.command}'")

        # For late-ack handlers, send ACK after dispatch with response payload
        if not use_early_ack:
            ack_packet = build_ack_packet(
                command_id, self._node_id, payload=response
            )
            self._last_command_id = command_id
            self._last_ack_packet = ack_packet
            success = self._send_ack(ack_packet, add_jitter)
            if success:
                logger.debug(
                    f"Sent ACK+payload for '{cmd.command}' (id: {command_id})"
                )
            else:
                logger.warning(f"Failed to send ACK for '{cmd.command}'")


def load_config(config_path: str) -> dict:
    """Load node configuration from JSON file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        return json.load(f)


def get_sensor_class(class_name: str) -> type[Sensor] | None:
    """
    Get a Sensor class by name using reflection.

    Args:
        class_name: Name of the sensor class (e.g., "BME280TempPressureHumidity")

    Returns:
        The sensor class, or None if not found
    """
    for name, obj in inspect.getmembers(sensors_module, inspect.isclass):
        if name == class_name and issubclass(obj, Sensor) and obj is not Sensor:
            return obj
    return None


def instantiate_sensors(
    sensor_configs: list[dict], default_interval: float
) -> list[SensorEntry]:
    """
    Instantiate sensors from configuration.

    Args:
        sensor_configs: List of sensor config dicts with 'class', optional 'config',
                        and optional 'interval_sec'
        default_interval: Default interval for sensors without explicit interval_sec

    Returns:
        List of SensorEntry objects for successfully initialized sensors
    """
    sensors = []

    for config in sensor_configs:
        class_name = config.get("class")
        if not class_name:
            logger.warning("Sensor config missing 'class' field, skipping")
            continue

        sensor_class = get_sensor_class(class_name)
        if sensor_class is None:
            logger.warning(f"Unknown sensor class: {class_name}, skipping")
            continue

        try:
            # Get optional constructor arguments
            kwargs = config.get("config", {})
            sensor = sensor_class(**kwargs)
            sensor.init()

            # Get per-sensor interval or use default
            interval = config.get("interval_sec", default_interval)

            entry = SensorEntry(sensor=sensor, interval_sec=interval)
            sensors.append(entry)
            logger.info(f"Initialized sensor: {entry.class_name} (interval: {interval}s)")
        except Exception as e:
            logger.error(f"Failed to initialize {class_name}: {e}")

    return sensors


def read_sensors(entries: list[SensorEntry]) -> list[SensorReading]:
    """
    Read specified sensors and build a list of readings.

    Args:
        entries: List of SensorEntry objects to read

    Returns:
        List of SensorReading objects with current timestamps
    """
    readings = []
    timestamp = time.time()

    for entry in entries:
        try:
            values = entry.sensor.read()
            names = entry.sensor.get_names()
            units = entry.sensor.get_units()
            precision = entry.sensor.get_precision()

            for value, name, unit in zip(values, names, units):
                readings.append(
                    SensorReading(
                        name=name,
                        units=unit,
                        value=value,
                        sensor_class=entry.class_name,
                        timestamp=timestamp,
                        precision=precision,
                    )
                )
        except Exception as e:
            logger.error(f"Error reading {entry.class_name}: {e}")

    return readings


def broadcast_loop(
    radio: RFM9xRadio,
    node_id: str,
    sensors: list[SensorEntry],
    node_state: NodeState | None = None,
    radio_lock: threading.Lock | None = None,
) -> None:
    """
    Main broadcast loop with per-sensor intervals.

    Continuously reads sensors and broadcasts via LoRa based on each
    sensor's configured interval.

    Args:
        radio: Initialized radio instance
        node_id: This node's identifier
        sensors: List of SensorEntry objects with interval configuration
        node_state: Optional shared state for display updates
        radio_lock: Optional lock for half-duplex coordination with CommandReceiver
    """
    logger.info(f"Starting broadcast loop for node '{node_id}'")
    logger.info(f"Radio: {radio.frequency_mhz} MHz, TX power: {radio.tx_power} dBm")

    for entry in sensors:
        logger.info(f"  {entry.class_name}: every {entry.interval_sec}s")

    broadcast_count = 0

    while not _shutdown_requested:
        now = time.time()

        # Find sensors that are due for broadcast
        due_sensors = [
            entry
            for entry in sensors
            if (now - entry.last_broadcast) >= entry.interval_sec
        ]

        if due_sensors:
            try:
                # Read only sensors that are due
                readings = read_sensors(due_sensors)

                # Update node state with latest readings for display
                if node_state and readings:
                    node_state.update_sensor_readings(
                        [
                            (r.name, r.value, r.units, r.sensor_class)
                            for r in readings
                        ]
                    )

                if readings:
                    # Build compact packets (auto-splits if too large)
                    packets = build_lora_packets(node_id, readings)

                    broadcast_count += 1
                    all_success = True
                    total_bytes = 0

                    for packet in packets:
                        # Acquire lock if using half-duplex coordination
                        if radio_lock:
                            with radio_lock:
                                # Set N2G frequency before sending (CommandReceiver leaves radio on G2N)
                                if node_state:
                                    radio.set_frequency(node_state.n2g_freq)
                                success = radio.send(packet)
                        else:
                            success = radio.send(packet)
                        total_bytes += len(packet)
                        if not success:
                            all_success = False

                    # Update last broadcast time for sensors we just read
                    for entry in due_sensors:
                        entry.last_broadcast = now

                    sensor_names = ", ".join(e.class_name for e in due_sensors)
                    if all_success:
                        # Update node state broadcast count
                        if node_state:
                            node_state.increment_broadcast_count()

                        logger.info(
                            f"Broadcast #{broadcast_count}: {len(readings)} readings "
                            f"from [{sensor_names}], "
                            f"{len(packets)} packet(s), {total_bytes} bytes"
                        )
                    else:
                        logger.warning(
                            f"Broadcast #{broadcast_count} failed [{sensor_names}]"
                        )
                else:
                    logger.warning("No sensor readings available")

            except Exception as e:
                logger.error(f"Broadcast error: {e}")

        # Calculate sleep time until next sensor is due
        sleep_times = []
        for entry in sensors:
            time_since_last = now - entry.last_broadcast
            time_until_next = entry.interval_sec - time_since_last
            sleep_times.append(max(0.1, time_until_next))

        sleep_time = min(sleep_times) if sleep_times else 1.0
        time.sleep(sleep_time)


def main():
    from utils.process_lock import acquire_lock

    acquire_lock("node")

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    parser = argparse.ArgumentParser(
        description="Outdoor sensor node broadcaster",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="config/node_config.json",
        help="Path to config file (default: config/node_config.json)",
    )
    args = parser.parse_args()

    # Load configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    node_id = config.get("node_id")
    if not node_id:
        logger.error("Config missing 'node_id'")
        sys.exit(1)

    # Get default broadcast interval
    default_interval = config.get("default_sensor_interval_sec", 30)

    # Initialize sensors
    sensor_configs = config.get("sensors", [])
    if not sensor_configs:
        logger.error("Config has no sensors defined")
        sys.exit(1)

    sensors = instantiate_sensors(sensor_configs, default_interval)
    if not sensors:
        logger.error("No sensors could be initialized")
        sys.exit(1)

    # Initialize radio with dual-channel support
    lora_config = config.get("lora", {})

    # Load frequencies - try Hz keys first, fall back to MHz for backwards compat
    if "n2g_frequency_hz" in lora_config:
        n2g_freq = lora_config["n2g_frequency_hz"] / 1e6  # Hz to MHz
    else:
        n2g_freq = lora_config.get("n2g_frequency_mhz", 915.0)

    if "g2n_frequency_hz" in lora_config:
        g2n_freq = lora_config["g2n_frequency_hz"] / 1e6  # Hz to MHz
    else:
        g2n_freq = lora_config.get("g2n_frequency_mhz", 915.5)

    tx_power = lora_config.get("tx_power", 23)
    spreading_factor = lora_config.get("spreading_factor", 7)
    bandwidth_code = lora_config.get("bandwidth", 0)
    bandwidth_hz = BW_HZ_MAP.get(bandwidth_code, 125000)

    radio = RFM9xRadio(
        frequency_mhz=n2g_freq,  # Start on N2G (sensor broadcasts + ACKs)
        tx_power=tx_power,
        cs_pin=LORA_CS_PIN,
        reset_pin=LORA_RESET_PIN,
    )
    # Apply SF/BW from config (radio.init() will use these)
    radio.spreading_factor = spreading_factor
    radio.signal_bandwidth = bandwidth_hz

    # Create RadioState (encapsulates radio hardware and frequencies)
    radio_state = RadioState(
        radio=radio,
        n2g_freq=n2g_freq,
        g2n_freq=g2n_freq,
    )

    # Create node state (shared state container for all components)
    node_state = NodeState(
        node_id=node_id,
        radio_state=radio_state,
        config_path=args.config,
    )

    # Initialize display if configured
    screen_manager = None
    display_advance_button = None
    action_button = None
    scroll_button = None
    display_config = config.get("display", {})

    if display_config.get("enabled", False):
        try:
            from gpiozero import Button

            from display import (
                OffPage,
                ScreenManager,
                SSD1306Display,
            )
            from node.display import (
                ArducamOCRPage,
                NodeInfoPage,
                SensorValuesPage,
            )

            display = SSD1306Display(
                i2c_port=display_config.get("i2c_port", 1),
                i2c_address=display_config.get("i2c_address", 0x3C),
            )
            pages = [
                OffPage(),
                SensorValuesPage(node_state, auto_scroll=display_config.get("auto_scroll", False)),
                NodeInfoPage(node_state),
                ArducamOCRPage(node_state),
            ]
            screen_manager = ScreenManager(
                display=display,
                pages=pages,
                refresh_interval=display_config.get("refresh_interval", 0.5),
            )
            screen_manager.start()

            # Page advance button
            if advance_pin := display_config.get("advance_switch_pin"):
                display_advance_button = Button(advance_pin, bounce_time=0.02)
                display_advance_button.when_pressed = screen_manager.advance_page

            # Action button (context-sensitive by page)
            if action_pin := display_config.get("action_switch_pin"):
                action_button = Button(action_pin, bounce_time=0.02)
                action_button.when_pressed = screen_manager.do_page_action

            # Scroll button (scroll within current page)
            if scroll_pin := display_config.get("scroll_switch_pin"):
                scroll_button = Button(scroll_pin, bounce_time=0.02)
                scroll_button.when_pressed = lambda: screen_manager.scroll_page(1)

            logger.info("OLED display initialized")
        except Exception as e:
            logger.warning(f"Failed to initialize display: {e}")

    # Initialize LED if configured
    led = None
    led_config = config.get("led", {})
    if led_config:
        try:
            from utils.led import RgbLed

            led = RgbLed(
                red_bcm=led_config.get("red_bcm", 17),
                green_bcm=led_config.get("green_bcm", 27),
                blue_bcm=led_config.get("blue_bcm", 22),
                common_anode=led_config.get("common_anode", True),
            )
            node_state.led = led
            node_state.default_brightness = led_config.get("default_brightness", 128)
            logger.info("LED initialized")
        except Exception as e:
            logger.warning(f"Failed to initialize LED: {e}")

    # Create command registry and register handlers
    command_registry = CommandRegistry(node_id)
    commands_init(command_registry, node_state)

    # Create radio lock for half-duplex coordination
    radio_lock: threading.Lock | None = None
    command_receiver: CommandReceiver | None = None

    # Check if command receiver is enabled
    command_config = config.get("command_receiver", {})
    command_receiver_enabled = command_config.get("enabled", False)

    if command_receiver_enabled:
        radio_lock = threading.Lock()

    try:
        radio.init()
        logger.info("Radio initialized")

        # Start command receiver if enabled
        if command_receiver_enabled and radio_lock is not None:
            receive_timeout = command_config.get("receive_timeout", 4.0)
            jitter_ms = command_config.get("broadcast_ack_jitter_ms", 500)
            command_receiver = CommandReceiver(
                radio=radio,
                radio_lock=radio_lock,
                node_id=node_id,
                registry=command_registry,
                receive_timeout=receive_timeout,
                radio_state=radio_state,
                broadcast_ack_jitter_sec=jitter_ms / 1000.0,
            )
            command_receiver.start()
            logger.info("Command receiver enabled")

        # Start broadcast loop
        broadcast_loop(radio, node_id, sensors, node_state, radio_lock)

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        logger.info("Cleaning up resources...")
        if command_receiver:
            command_receiver.stop()
        if screen_manager:
            screen_manager.close()
        if display_advance_button:
            display_advance_button.close()
        if action_button:
            action_button.close()
        if led:
            led.close()
        for entry in sensors:
            entry.sensor.close()
        radio.close()
        logger.info("Cleanup complete")


if __name__ == "__main__":
    main()
