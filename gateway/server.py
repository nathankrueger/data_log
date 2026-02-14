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
import signal
import sys
import time
from pathlib import Path

from gpiozero import Button

from display import (
    GatewayLocalSensors,
    LastPacketPage,
    OffPage,
    ScreenManager,
    SSD1306Display,
    SystemInfoPage,
)
from gateway.command_queue import CommandQueue
from gateway.http_handler import CommandServer
from gateway.sensor_collection import (
    DashboardClient,
    LocalSensorReader,
    SensorDataCollector,
    instantiate_sensors,
)
from gateway.transceiver import LoRaTransceiver
from radio import RFM9xRadio
from utils.gateway_state import GatewayState
from utils.led import RgbLed
from utils.radio_state import RadioState

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
cmd_logger = logging.getLogger("cmd_debug")


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
        min_retry_ms=command_config.get("min_retry_ms", 0),
        max_retry_ms=command_config.get("max_retry_ms", 5000),
        retry_multiplier=command_config.get("retry_multiplier", 1.5),
        discovery_retries=command_config.get("discovery_retries", 30),
        wait_timeout=command_config.get("wait_timeout", 30.0),
    )
    command_queue.validate_timeouts()  # Warn if wait_timeout < max_retry_time
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

            # Apply SF and BW from config if present (overrides rfm9x.py defaults)
            if "spreading_factor" in lora_config:
                radio.spreading_factor = lora_config["spreading_factor"]
            if "signal_bandwidth" in lora_config:
                radio.signal_bandwidth = lora_config["signal_bandwidth"]

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

    def on_sigterm(signum, frame):
        logger.info("Received SIGTERM - initiating shutdown")
        raise KeyboardInterrupt()

    signal.signal(signal.SIGUSR1, enable_flash)
    signal.signal(signal.SIGUSR2, disable_flash)
    signal.signal(signal.SIGTERM, on_sigterm)
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
