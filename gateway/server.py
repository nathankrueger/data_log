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
        {"class": "BME280TempPressureHumidity"},
        {"class": "LoRaRSSI", "interval_sec": 0, "config": {"nodes": ["ab01-7"]}}
    ],
    "local_sensor_interval_sec": 5,

Each local sensor may set its own "interval_sec" (default:
local_sensor_interval_sec). LoRaRSSI/LoRaSNR log link quality for the listed
nodes only; interval_sec 0 logs every packet, N logs at most the latest packet
per N seconds (and nothing if none arrived).
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
import logging
import signal
import sys
import time
from pathlib import Path

from gateway.command_queue import CommandQueue
from gateway.sensor_collection import (
    FEED_SIZE_DEFAULT,
    DashboardClient,
    LocalSensorReader,
    SensorDataCollector,
    instantiate_sensors,
)
from gateway.timeseries import LoggerService
from gateway.web import WebServer
from utils.gateway_state import GatewayState

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOGGER_DIR = "data/logger"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
cmd_logger = logging.getLogger("cmd_debug")


# Watchdog: how often the main loop checks components, and how long the LoRa
# loop may go without a heartbeat (discovery, slow SF12 transmissions and
# radio reconnect attempts all beat at least every ~12 s) before the gateway
# restarts itself.
HEALTH_CHECK_INTERVAL_S = 10.0
TRANSCEIVER_STALL_S = 120.0


def load_config(config_path: str) -> dict:
    """Load gateway configuration from JSON file (falls back to the .bak copy
    kept by utils.config_persistence if the file was corrupted)."""
    from utils.config_persistence import load_json_with_backup

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    return load_json_with_backup(path)


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

    # Create shared state for gateway components
    gateway_state = GatewayState()
    gateway_state.node_id = node_id
    gateway_state.config_path = config_path
    gateway_state.dashboard_url = dashboard_url or ""

    # Create dashboard client and collector
    web_config = config.get("web", {})
    dashboard_client = DashboardClient(dashboard_url, node_id) if dashboard_url else None
    collector = SensorDataCollector(
        node_id,
        dashboard_client,
        feed_size=web_config.get("feed_size", FEED_SIZE_DEFAULT),
    )
    collector.start()

    # On-gateway history logger. Idle (no thread, no sqlite) unless recording
    # was switched on from the web UI; that choice persists in its data dir.
    logger_config = config.get("logger", {})
    logger_dir = Path(logger_config.get("data_dir", DEFAULT_LOGGER_DIR)).expanduser()
    if not logger_dir.is_absolute():
        logger_dir = PROJECT_ROOT / logger_dir
    logger_service = LoggerService(
        data_dir=logger_dir,
        collector=collector,
        flush_interval=logger_config.get("flush_interval_s", 30.0),
        prune_interval=logger_config.get("prune_interval_s", 300.0),
    )
    logger_service.start()
    if logger_service.recording:
        logger.info(f"History recording active ({logger_dir})")

    if dashboard_url:
        logger.info(f"Gateway '{node_id}' posting to {dashboard_url}")
    else:
        logger.info(f"Gateway '{node_id}' running without dashboard (no dashboard_url configured)")

    # Initialize LED if configured
    led = None
    led_config = config.get("led", {})
    if led_config.get("enabled", bool(led_config)):
        try:
            from utils.led import RgbLed

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

    # Start web server (command API + browser UI). Wire everything that exists
    # now so the UI works even if the radio fails to come up; radio params are
    # added to the registry once LoRa is initialized below.
    port = command_config.get("port", 5001)
    command_server = WebServer(
        port=port,
        command_queue=command_queue,
        discovery_config=discovery_config,
        config_path=config_path,
    )
    command_server.set_sensor_collector(collector)
    command_server.set_logger_service(logger_service)
    command_server.set_node_order(web_config.get("node_order", []))
    command_server.set_gateway_state(gateway_state)
    command_server.start()
    logger.info(f"Web UI and command API on port {port}")

    # Instantiate local sensors before LoRa: the transceiver feeds the
    # LoRaRSSI/LoRaSNR ones. The reader thread is started further down.
    local_entries = instantiate_sensors(
        config.get("local_sensors", []),
        default_interval=config.get("local_sensor_interval_sec", 5.0),
    )
    link_sensors = [e.sensor for e in local_entries if e.is_link]

    # Initialize LoRa transceiver
    exit_code = 0
    lora_transceiver = None
    radio = None
    lora_config = config.get("lora", {})

    try:
        from gateway.transceiver import LoRaTransceiver
        from radio import RFM9xRadio
        from utils.radio_state import RadioState

        # Dual-channel: N2G for sensors+ACKs, G2N for commands
        n2g_freq = lora_config.get("n2g_frequency_mhz", 915.0)
        g2n_freq = lora_config.get("g2n_frequency_mhz", 915.5)

        radio = RFM9xRadio(
            frequency_mhz=n2g_freq,  # Start on N2G (sensors + ACKs)
            tx_power=lora_config.get("tx_power", 23),
            spreading_factor=lora_config.get("spreading_factor", 7),
            signal_bandwidth=lora_config.get("signal_bandwidth", 125000),
            cs_pin=lora_config.get("cs_pin", 24),
            reset_pin=lora_config.get("reset_pin", 25),
            backend=lora_config.get("backend", "rpi"),
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
            link_sensors=link_sensors,
        )
        lora_transceiver.set_flash_enabled(flash_on_recv_default)
        lora_transceiver.silence_reset_s = float(
            lora_config.get("silence_reset_s", lora_transceiver.silence_reset_s)
        )
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
        # Wire transceiver and rebuild the param registry with radio params
        command_server.set_transceiver(lora_transceiver)
        command_server.set_gateway_state(gateway_state)
    except Exception as e:
        logger.error(f"Failed to initialize LoRa: {e}")
        if lora_config.get("required", True):
            # A LoRa gateway without its radio is down, however healthy the
            # web UI looks. Exit non-zero (after the cleanup below) so systemd
            # or scripts/launch_gateway_server.sh retries until it works,
            # e.g. once the FT232H adapter is plugged in.
            exit_code = 1
        else:
            logger.info("Continuing without LoRa transceiver (lora.required is false)")

    if link_sensors and lora_transceiver is None:
        logger.warning("LoRa link sensors configured but LoRa is unavailable")

    # Start local sensor reader if configured
    local_reader = None
    if local_entries:
        local_reader = LocalSensorReader(
            node_id, local_entries, collector, gateway_state
        )
        local_reader.start()

    # Initialize OLED display if configured
    screen_manager = None
    display_advance_button = None
    display_scroll_button = None
    display_config = config.get("display", {})

    if display_config.get("enabled", False):
        try:
            from gpiozero import Button

            from display import OffPage, ScreenManager, SSD1306Display
            from gateway.display_pages import GatewayLocalSensors, LastPacketPage, SystemInfoPage

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

    # Run forever (threads are daemon threads, so Ctrl+C will stop everything).
    # The main thread doubles as a watchdog: things that can be fixed in
    # process are (history recorder); a LoRa path that is dead or wedged ends
    # the process with a non-zero code so the supervisor restarts it cleanly.
    try:
        next_health_check = time.monotonic() + HEALTH_CHECK_INTERVAL_S
        while exit_code == 0:
            time.sleep(1)
            if time.monotonic() < next_health_check:
                continue
            next_health_check = time.monotonic() + HEALTH_CHECK_INTERVAL_S
            try:
                logger_service.check_health()
                if not command_server.is_alive():
                    logger.critical("Web server thread has exited; command API and UI are down")
                if lora_transceiver is not None:
                    stalled = time.monotonic() - lora_transceiver.heartbeat
                    if not lora_transceiver.is_alive():
                        logger.critical("LoRa transceiver has stopped; restarting the gateway")
                        exit_code = 1
                    elif stalled > TRANSCEIVER_STALL_S:
                        logger.critical(
                            f"LoRa transceiver stalled for {stalled:.0f}s; restarting the gateway"
                        )
                        exit_code = 1
            except Exception as e:  # noqa: BLE001 - the watchdog itself must never die
                logger.error(f"Health check failed: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        # A second SIGTERM must not abort cleanup (and the final history flush)
        try:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
        logger.info("Shutting down...")

        def step(name, fn):
            # Each step isolated: one failure must not skip the rest
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                logger.error(f"Shutdown step '{name}' failed: {e}")

        if lora_transceiver:
            step("transceiver", lora_transceiver.stop)  # joins (bounded)
        if command_server:
            step("web server", command_server.stop)
        if local_reader:
            step("local sensors", local_reader.stop)
        step("history logger", logger_service.stop)  # final flush of buffered samples
        step("collector", collector.stop)
        if screen_manager:
            step("display", screen_manager.close)
        if display_advance_button:
            step("advance button", display_advance_button.close)
        if display_scroll_button:
            step("scroll button", display_scroll_button.close)
        if radio:
            if lora_transceiver is not None and lora_transceiver.is_alive():
                # Still inside an SPI/USB call: closing the bus under it could hang
                logger.warning("Transceiver did not stop; leaving the radio as is")
            else:
                step("radio", radio.close)
        if led:
            step("led", led.close)

    if exit_code:
        logging.shutdown()
        sys.exit(exit_code)


def main():
    from utils.process_lock import acquire_lock

    acquire_lock("gateway")

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
    except ValueError as e:  # JSON syntax error with no usable .bak
        logger.critical(f"Config file {args.config} is not valid JSON and has no usable backup: {e}")
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
