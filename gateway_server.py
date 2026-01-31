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
    python3 gateway_server.py [config_file]
"""

import argparse
import json
import logging
import inspect
import signal
import sys
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from utils.led import RgbLed
from utils.gateway_state import GatewayState
from utils.display import (
    LastPacketPage,
    OffPage,
    ScreenManager,
    SystemInfoPage,
)

import sensors as sensors_module
from radio import RFM9xRadio
from sensors import Sensor
from utils.protocol import (
    SensorReading,
    make_sensor_id,
    parse_lora_packet,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# =============================================================================
# Dashboard Client
# =============================================================================


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
    """

    def __init__(self, gateway_id: str, dashboard_client: DashboardClient):
        self._gateway_id = gateway_id
        self._dashboard_client = dashboard_client
        self._lock = threading.Lock()
        # Buffer readings for batch posting
        self._pending_readings: list[dict] = []

    def add_readings(self, node_id: str, readings: list[SensorReading], is_local: bool = False) -> None:
        """
        Add sensor readings to the pending buffer and post to dashboard.

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

        # Post immediately
        if datapoints:
            success = self._dashboard_client.post_readings(datapoints)
            if success:
                logger.info(f"Posted {len(datapoints)} readings from '{node_id}'")
            else:
                logger.warning(f"Failed to post {len(datapoints)} readings from '{node_id}'")

    @property
    def gateway_id(self) -> str:
        return self._gateway_id


# =============================================================================
# LoRa Receiver Thread
# =============================================================================


class LoRaReceiver(threading.Thread):
    """Background thread that receives LoRa packets and forwards to collector."""

    def __init__(
        self,
        radio: RFM9xRadio,
        collector: SensorDataCollector,
        led: RgbLed | None = None,
        flash_color: tuple[int, int, int] = (255, 0, 0),
        flash_duration: float = 0.1,
        gateway_state: GatewayState | None = None,
    ):
        super().__init__(daemon=True)
        self._radio = radio
        self._collector = collector
        self._led = led
        self._flash_color = flash_color
        self._flash_duration = flash_duration
        self._flash_enabled = True  # Can be toggled via signals
        self._running = False
        self._gateway_state = gateway_state

    def set_flash_enabled(self, enabled: bool) -> None:
        """Enable or disable LED flash on receive."""
        self._flash_enabled = enabled
        logger.info(f"LED flash on receive: {'enabled' if enabled else 'disabled'}")

    def run(self) -> None:
        self._running = True
        logger.info("LoRa receiver started")

        while self._running:
            try:
                packet = self._radio.receive(timeout=5.0)
                if packet is not None:
                    self._process_packet(packet)
            except Exception as e:
                logger.error(f"LoRa receive error: {e}")
                time.sleep(1)  # Back off on error

    def stop(self) -> None:
        self._running = False

    def _process_packet(self, packet: bytes) -> None:
        """Validate CRC, parse JSON, forward to collector."""
        rssi = self._radio.get_last_rssi()

        result = parse_lora_packet(packet)
        if result is None:
            logger.warning(f"Invalid LoRa packet (RSSI: {rssi} dB): {packet[:50]}...")
            return

        node_id, readings = result
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
    ):
        super().__init__(daemon=True)
        self._node_id = node_id
        self._sensors = sensors
        self._collector = collector
        self._interval_sec = interval_sec
        self._running = False

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


def run_gateway(config: dict) -> None:
    """Run the gateway."""
    node_id = config.get("node_id", "gateway")
    dashboard_url = config.get("dashboard_url")

    if not dashboard_url:
        logger.error("dashboard_url not configured")
        sys.exit(1)

    # Create shared state for display
    gateway_state = GatewayState()

    # Create dashboard client and collector
    dashboard_client = DashboardClient(dashboard_url, node_id)
    collector = SensorDataCollector(node_id, dashboard_client)

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

    # Start LoRa receiver if enabled
    lora_receiver = None
    radio = None
    lora_config = config.get("lora", {})

    if lora_config.get("enabled", True):
        try:
            radio = RFM9xRadio(
                frequency_mhz=lora_config.get("frequency_mhz", 915.0),
                tx_power=lora_config.get("tx_power", 23),
                cs_pin=lora_config.get("cs_pin", 24),
                reset_pin=lora_config.get("reset_pin", 25),
            )
            radio.init()
            lora_receiver = LoRaReceiver(
                radio,
                collector,
                led=led,
                flash_color=flash_color,
                flash_duration=flash_duration,
                gateway_state=gateway_state,
            )
            lora_receiver.set_flash_enabled(flash_on_recv_default)
            lora_receiver.start()
            logger.info(f"LoRa receiver enabled at {radio.frequency_mhz} MHz")
        except Exception as e:
            logger.error(f"Failed to initialize LoRa: {e}")
            logger.info("Continuing without LoRa receiver")

    # Set up signal handlers for runtime LED toggle
    def enable_flash(signum, frame):
        logger.info("Received SIGUSR1 signal")
        if lora_receiver:
            lora_receiver.set_flash_enabled(True)
        else:
            logger.warning("No LoRa receiver to enable flash on")

    def disable_flash(signum, frame):
        logger.info("Received SIGUSR2 signal")
        if lora_receiver:
            lora_receiver.set_flash_enabled(False)
        else:
            logger.warning("No LoRa receiver to disable flash on")

    signal.signal(signal.SIGUSR1, enable_flash)
    signal.signal(signal.SIGUSR2, disable_flash)
    logger.info("Signal handlers registered (SIGUSR1=enable LED, SIGUSR2=disable LED)")

    # Start local sensor reader if configured
    local_reader = None
    local_sensor_configs = config.get("local_sensors", [])

    if local_sensor_configs:
        local_sensors = instantiate_sensors(local_sensor_configs)
        if local_sensors:
            interval = config.get("local_sensor_interval_sec", 5.0)
            local_reader = LocalSensorReader(node_id, local_sensors, collector, interval)
            local_reader.start()

    # Initialize OLED display if configured
    screen_manager = None
    display_config = config.get("display", {})

    if display_config.get("enabled", False):
        try:
            pages = [
                OffPage(),
                SystemInfoPage(gateway_state),
                LastPacketPage(gateway_state),
            ]
            screen_manager = ScreenManager(
                pages=pages,
                switch_pin=display_config.get("switch_pin", 16),
                i2c_port=display_config.get("i2c_port", 1),
                i2c_address=display_config.get("i2c_address", 0x3C),
                refresh_interval=display_config.get("refresh_interval", 0.5),
            )
            screen_manager.start()
            logger.info("OLED display initialized (switch on GPIO to cycle pages)")
        except Exception as e:
            logger.warning(f"Failed to initialize display: {e}")
            screen_manager = None

    # Run forever (threads are daemon threads, so Ctrl+C will stop everything)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        # Cleanup
        logger.info("Shutting down...")
        if lora_receiver:
            lora_receiver.stop()
        if local_reader:
            local_reader.stop()
        if screen_manager:
            screen_manager.close()
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
    args = parser.parse_args()

    # Load configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    # Run gateway
    run_gateway(config)


if __name__ == "__main__":
    main()
