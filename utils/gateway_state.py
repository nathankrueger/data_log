"""
Shared runtime state for the gateway.

This module provides thread-safe state classes that can be shared
between gateway components (LoRa receiver, display, etc.).
"""

import threading
import time
from dataclasses import dataclass, field


@dataclass
class LastPacketInfo:
    """Information about the last received packet."""

    timestamp: float = 0.0
    node_id: str = ""
    rssi: int = 0
    sensor_name: str = ""
    sensor_value: float = 0.0
    sensor_units: str = ""


@dataclass
class LocalSensorReading:
    """A single local sensor reading."""

    name: str = ""
    value: float = 0.0
    units: str = ""


@dataclass
class GatewayState:
    """
    Shared runtime state for the gateway.

    Thread-safe container for state that multiple components need to access,
    such as the display pages and LoRa receiver.
    """

    start_time: float = field(default_factory=time.time)
    last_packet: LastPacketInfo = field(default_factory=LastPacketInfo)
    local_sensors: list[LocalSensorReading] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update_last_packet(
        self,
        node_id: str,
        rssi: int,
        sensor_name: str,
        sensor_value: float,
        sensor_units: str,
    ) -> None:
        """Update last packet info (thread-safe)."""
        with self._lock:
            self.last_packet.timestamp = time.time()
            self.last_packet.node_id = node_id
            self.last_packet.rssi = rssi
            self.last_packet.sensor_name = sensor_name
            self.last_packet.sensor_value = sensor_value
            self.last_packet.sensor_units = sensor_units

    def get_last_packet(self) -> LastPacketInfo:
        """Get a copy of last packet info (thread-safe)."""
        with self._lock:
            return LastPacketInfo(
                timestamp=self.last_packet.timestamp,
                node_id=self.last_packet.node_id,
                rssi=self.last_packet.rssi,
                sensor_name=self.last_packet.sensor_name,
                sensor_value=self.last_packet.sensor_value,
                sensor_units=self.last_packet.sensor_units,
            )

    def update_local_sensors(self, readings: list[tuple[str, float, str]]) -> None:
        """
        Update local sensor readings (thread-safe).

        Args:
            readings: List of (name, value, units) tuples
        """
        with self._lock:
            self.local_sensors = [
                LocalSensorReading(name=name, value=value, units=units)
                for name, value, units in readings
            ]

    def get_local_sensors(self) -> list[LocalSensorReading]:
        """Get a copy of local sensor readings (thread-safe)."""
        with self._lock:
            return [
                LocalSensorReading(name=r.name, value=r.value, units=r.units)
                for r in self.local_sensors
            ]
