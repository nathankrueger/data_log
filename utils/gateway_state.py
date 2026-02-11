"""
Shared runtime state for the gateway.

This module provides thread-safe state classes that can be shared
between gateway components (LoRa receiver, display, HTTP server, etc.).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from radio import Radio
    from utils.radio_state import RadioState


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
    such as the display pages, LoRa receiver, and HTTP server.

    Uses RadioState for radio configuration (shared with node implementation).
    """

    # Display-related fields
    start_time: float = field(default_factory=time.time)
    last_packet: LastPacketInfo = field(default_factory=LastPacketInfo)
    local_sensors: list[LocalSensorReading] = field(default_factory=list)
    dashboard_url: str = ""

    # Infrastructure references
    node_id: str = ""
    config_path: str = ""
    radio_state: RadioState | None = None  # Shared RadioState class
    command_queue: Any = None  # CommandQueue (avoid circular import)

    _lock: threading.Lock = field(default_factory=threading.Lock)

    # Convenience accessors for radio properties
    @property
    def radio(self) -> Radio | None:
        """Get the radio hardware instance."""
        return self.radio_state.radio if self.radio_state else None

    @property
    def n2g_freq(self) -> float:
        """Get Node-to-Gateway frequency in MHz."""
        return self.radio_state.n2g_freq if self.radio_state else 915.0

    @property
    def g2n_freq(self) -> float:
        """Get Gateway-to-Node frequency in MHz."""
        return self.radio_state.g2n_freq if self.radio_state else 915.5

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
