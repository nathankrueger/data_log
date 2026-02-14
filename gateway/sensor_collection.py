"""
Sensor data collection and dashboard posting for the gateway.

Classes:
    PendingPost: Data container for readings waiting to be posted
    DashboardClient: HTTP client for posting sensor data to dashboard
    SensorDataCollector: Collects readings from LoRa and local sources
    LocalSensorReader: Background thread for reading local sensors

Functions:
    get_sensor_class: Get a Sensor class by name using reflection
    instantiate_sensors: Create Sensor instances from configuration
"""

import inspect
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import sensors as sensors_module
from sensors import Sensor
from utils.gateway_state import GatewayState
from utils.protocol import SensorReading, make_sensor_id

logger = logging.getLogger(__name__)


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
                values = sensor.transform(sensor.read())
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
# Sensor Instantiation Helpers
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
