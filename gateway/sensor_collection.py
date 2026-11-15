"""
Sensor data collection and dashboard posting for the gateway.

Classes:
    PendingPost: Data container for readings waiting to be posted
    DashboardClient: HTTP client for posting sensor data to dashboard
    SensorDataCollector: Collects readings from LoRa and local sources
    LocalSensorEntry: A configured local sensor and its publish schedule
    LocalSensorReader: Background thread that publishes local sensors

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
from collections import deque
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import sensors as sensors_module
from sensors import EVERY_PACKET, LoRaLinkSensor, Sensor
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
        dashboard_client: DashboardClient | None,
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
        self._recent_readings: deque[dict] = deque(maxlen=100)

    def start(self) -> None:
        """Start the background poster thread."""
        if self._running:
            return
        self._running = True
        if self._dashboard_client is None:
            logger.info("No dashboard client configured, skipping poster thread")
            return
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

        for dp in datapoints:
            self._recent_readings.append(dp)

        if self._dashboard_client is None:
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

    def get_recent_readings(self) -> list[dict]:
        """Return a snapshot of recent readings (newest last)."""
        return list(self._recent_readings)

    @property
    def gateway_id(self) -> str:
        return self._gateway_id


# =============================================================================
# Local Sensor Reader Thread
# =============================================================================


@dataclass
class LocalSensorEntry:
    """A configured gateway local sensor and its publish schedule."""

    sensor: Sensor
    class_name: str
    interval_sec: float
    next_due: float = 0.0  # 0 = due immediately at startup

    @property
    def is_link(self) -> bool:
        """True for LoRa link metrics (fed by the transceiver, not polled)."""
        return isinstance(self.sensor, LoRaLinkSensor)

    @property
    def every_packet(self) -> bool:
        """True if every received packet is published (no rate limit)."""
        return self.is_link and self.interval_sec == EVERY_PACKET


class LocalSensorReader(threading.Thread):
    """
    Background thread that publishes gateway local sensors to the collector.

    Each entry runs on its own ``interval_sec``:
    - Hardware sensors are polled when due and posted under the gateway's
      node ID.
    - LoRa link sensors (LoRaRSSI/LoRaSNR) are drained when due -- publishing
      each node's latest sample, or nothing if no packet arrived since the last
      publish -- and posted under the node each sample describes.
    - Link sensors with interval_sec == EVERY_PACKET (0) are drained as soon as
      the transceiver records a packet (it sets our wake event), so every
      packet is published.
    """

    def __init__(
        self,
        node_id: str,
        entries: list[LocalSensorEntry],
        collector: SensorDataCollector,
        gateway_state: GatewayState | None = None,
    ):
        super().__init__(daemon=True, name="LocalSensorReader")
        self._node_id = node_id
        self._entries = entries
        self._collector = collector
        self._running = False
        self._gateway_state = gateway_state
        self._wake = threading.Event()
        for entry in entries:
            if entry.every_packet:
                entry.sensor.set_wake_event(self._wake)

    def run(self) -> None:
        self._running = True
        for entry in self._entries:
            schedule = (
                "every packet" if entry.every_packet
                else f"every {entry.interval_sec}s"
            )
            nodes = (
                f" nodes={sorted(entry.sensor.nodes)}" if entry.is_link else ""
            )
            logger.info(f"Local sensor {entry.class_name}: {schedule}{nodes}")

        while self._running:
            # Clear before the pass: a packet recorded mid-pass sets the event
            # again, so the wait below returns immediately instead of losing it.
            self._wake.clear()
            self._run_due(time.time())
            self._wake.wait(self._time_until_next_due())

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def _run_due(self, now: float) -> None:
        """Publish every entry that is due at ``now``."""
        hw_readings: list[SensorReading] = []

        for entry in self._entries:
            try:
                if entry.every_packet:
                    self._publish_link(entry)
                    continue
                if now < entry.next_due:
                    continue
                entry.next_due = now + entry.interval_sec
                if entry.is_link:
                    self._publish_link(entry)
                else:
                    hw_readings.extend(self._read_sensor(entry, now))
            except Exception as e:
                logger.error(f"Local sensor {entry.class_name} error: {e}")

        if hw_readings:
            self._collector.add_readings(self._node_id, hw_readings, is_local=True)
            # Update gateway state for display
            if self._gateway_state:
                self._gateway_state.update_local_sensors([
                    (r.name, r.value, r.units) for r in hw_readings
                ])

    def _publish_link(self, entry: LocalSensorEntry) -> None:
        """Post a link sensor's pending samples, each under its own node."""
        for node_id, readings in entry.sensor.drain(latest_only=not entry.every_packet):
            self._collector.add_readings(node_id, readings, is_local=False)

    def _time_until_next_due(self) -> float | None:
        """Seconds until the next timed entry is due (None = only wake events)."""
        timed = [e.next_due for e in self._entries if not e.every_packet]
        if not timed:
            return None
        return max(0.0, min(timed) - time.time())

    @staticmethod
    def _read_sensor(entry: LocalSensorEntry, timestamp: float) -> list[SensorReading]:
        """Poll one hardware sensor."""
        sensor = entry.sensor
        values = sensor.transform(sensor.read())
        return [
            SensorReading(
                name=name,
                units=unit,
                value=value,
                sensor_class=entry.class_name,
                timestamp=timestamp,
            )
            for value, name, unit in zip(values, sensor.get_names(), sensor.get_units())
        ]


# =============================================================================
# Sensor Instantiation Helpers
# =============================================================================


def get_sensor_class(class_name: str) -> type[Sensor] | None:
    """Get a Sensor class by name using reflection."""
    for name, obj in inspect.getmembers(sensors_module, inspect.isclass):
        if name == class_name and issubclass(obj, Sensor) and obj is not Sensor:
            return obj
    return None


def instantiate_sensors(
    sensor_configs: list[dict], default_interval: float = 5.0
) -> list[LocalSensorEntry]:
    """Instantiate local sensors from configuration.

    Each config dict has ``class``, optional ``config`` (constructor kwargs)
    and optional ``interval_sec`` (defaults to ``default_interval``). An
    interval of 0 (EVERY_PACKET) is only meaningful for LoRa link sensors.
    """
    entries = []

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
        except Exception as e:
            logger.error(f"Failed to initialize {class_name}: {e}")
            continue

        interval = config.get("interval_sec", default_interval)
        is_link = isinstance(sensor, LoRaLinkSensor)
        valid = (
            isinstance(interval, (int, float))
            and not isinstance(interval, bool)
            and (interval > 0 or (is_link and interval == EVERY_PACKET))
        )
        if not valid:
            logger.warning(
                f"{class_name}: invalid interval_sec {interval!r} "
                f"(0 = every packet, LoRa link sensors only); "
                f"using {default_interval}s"
            )
            interval = default_interval

        entries.append(LocalSensorEntry(sensor, class_name, interval))
        logger.info(f"Initialized local sensor: {class_name}")

    return entries
