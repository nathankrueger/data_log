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
    sensor_name: str = ""
    sensor_value: float = 0.0
    sensor_units: str = ""


@dataclass
class GatewayState:
    """
    Shared runtime state for the gateway.

    Thread-safe container for state that multiple components need to access,
    such as the display pages and LoRa receiver.
    """

    start_time: float = field(default_factory=time.time)
    last_packet: LastPacketInfo = field(default_factory=LastPacketInfo)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update_last_packet(
        self,
        node_id: str,
        sensor_name: str,
        sensor_value: float,
        sensor_units: str,
    ) -> None:
        """Update last packet info (thread-safe)."""
        with self._lock:
            self.last_packet.timestamp = time.time()
            self.last_packet.node_id = node_id
            self.last_packet.sensor_name = sensor_name
            self.last_packet.sensor_value = sensor_value
            self.last_packet.sensor_units = sensor_units

    def get_last_packet(self) -> LastPacketInfo:
        """Get a copy of last packet info (thread-safe)."""
        with self._lock:
            return LastPacketInfo(
                timestamp=self.last_packet.timestamp,
                node_id=self.last_packet.node_id,
                sensor_name=self.last_packet.sensor_name,
                sensor_value=self.last_packet.sensor_value,
                sensor_units=self.last_packet.sensor_units,
            )
