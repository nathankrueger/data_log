"""
LoRa link-quality "sensors" measured by the gateway radio.

These aren't hardware drivers. The gateway's transceiver measures RSSI/SNR for
every packet it receives; these classes let that measurement be configured and
logged like any other gateway local sensor:

    "local_sensors": [
        {"class": "LoRaRSSI", "interval_sec": 0, "config": {"nodes": ["ab01-7"]}},
        {"class": "LoRaSNR", "interval_sec": 600, "config": {"nodes": ["ab01-8"]}}
    ]

Only the listed nodes are logged; ``nodes`` is required. Each logged value is
attributed to the node it describes, so it lands in the dashboard as e.g.
``ab01_7_lorarssi_rssi`` ("ab01-7 RSSI").

Publishing rate is set by the entry's ``interval_sec`` (see LocalSensorReader):
- ``0`` (EVERY_PACKET): every received packet is logged.
- ``N > 0``: at most one value per node every N seconds -- the most recently
  received one -- and nothing if no packet arrived since the last publish, so
  asking for a faster rate than the node transmits never logs repeats.

Data flow: LoRaTransceiver -> record_packet() (transceiver thread) -> pending
buffer -> drain() (LocalSensorReader thread) -> SensorDataCollector.
"""

import logging
import threading
from collections import deque

from .base import Sensor

logger = logging.getLogger(__name__)

# interval_sec value meaning "log every packet received from the node"
EVERY_PACKET = 0

# Per-node cap on samples held between drains. Drains happen promptly, so this
# only matters if the reader thread stalls; oldest samples are dropped first.
MAX_PENDING_PER_NODE = 256


class LoRaLinkSensor(Sensor):
    """Base class for per-node link metrics measured at the gateway."""

    # Overridden by subclasses
    reading_name: str = ""
    reading_units: str = ""

    def __init__(self, nodes: list[str] | None = None):
        if not nodes:
            raise ValueError(
                f"{type(self).__name__} requires a non-empty 'nodes' list "
                f"in its config (the node IDs to log)"
            )
        self._nodes = frozenset(nodes)
        self._pending: dict[str, deque[tuple[float, float]]] = {}
        self._lock = threading.Lock()
        self._wake_event: threading.Event | None = None

    @property
    def nodes(self) -> frozenset[str]:
        return self._nodes

    def set_wake_event(self, event: threading.Event | None) -> None:
        """Event to set whenever a sample is recorded (wakes the reader)."""
        self._wake_event = event

    def _select(self, rssi: float | None, snr: float | None) -> float | None:
        """Pick this metric's value out of a packet's measurements."""
        raise NotImplementedError

    def record_packet(
        self,
        node_id: str,
        rssi: float | None,
        snr: float | None,
        timestamp: float,
    ) -> None:
        """Record one received packet's measurements. Called by the transceiver."""
        if node_id not in self._nodes:
            return
        value = self._select(rssi, snr)
        if value is None:
            return
        with self._lock:
            queue = self._pending.get(node_id)
            if queue is None:
                queue = self._pending[node_id] = deque(maxlen=MAX_PENDING_PER_NODE)
            queue.append((timestamp, value))
        if self._wake_event is not None:
            self._wake_event.set()

    def has_pending(self) -> bool:
        with self._lock:
            return any(self._pending.values())

    def drain(self, latest_only: bool) -> list[tuple[str, list["SensorReading"]]]:
        """Take all pending samples, grouped by node.

        Args:
            latest_only: keep only each node's most recent sample (rate-limited
                mode); otherwise return every sample (every-packet mode).

        Returns:
            [(node_id, readings), ...] -- empty if nothing arrived since the
            last drain. Each reading's timestamp is the packet's receive time.
        """
        # Lazy import (like utils.protocol's own lazy `sensors` import): the
        # repo root is also a package whose __init__ imports `sensors` before
        # `utils` is importable.
        from utils.protocol import SensorReading

        with self._lock:
            pending = self._pending
            self._pending = {}

        class_name = type(self).__name__
        result = []
        for node_id, samples in pending.items():
            if not samples:
                continue
            chosen = [samples[-1]] if latest_only else list(samples)
            result.append((
                node_id,
                [
                    SensorReading(
                        name=self.reading_name,
                        units=self.reading_units,
                        value=value,
                        sensor_class=class_name,
                        timestamp=ts,
                    )
                    for ts, value in chosen
                ],
            ))
        return result

    # --- Sensor ABC: these sensors are fed, not polled ---

    def init(self) -> None:
        pass

    def read(self) -> tuple:
        # Not polled; values arrive via record_packet()/drain(). Returning an
        # empty tuple makes accidental polling (e.g. listed in a node config)
        # a harmless no-op.
        return ()

    def get_names(self) -> tuple[str, ...]:
        return ()

    def get_units(self) -> tuple[str, ...]:
        return ()


class LoRaRSSI(LoRaLinkSensor):
    """Received signal strength (dBm) of packets from the listed nodes."""

    reading_name = "RSSI"
    reading_units = "dBm"

    def _select(self, rssi, snr):
        return rssi


class LoRaSNR(LoRaLinkSensor):
    """Signal-to-noise ratio (dB) of packets from the listed nodes."""

    reading_name = "SNR"
    reading_units = "dB"

    def _select(self, rssi, snr):
        return snr
