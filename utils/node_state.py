"""
Shared runtime state for the sensor node.

This module provides thread-safe state classes that can be shared
between node components (broadcast loop, display, OCR, etc.).
"""

import threading
import time
from dataclasses import dataclass, field


@dataclass
class SensorReadingInfo:
    """A sensor reading for display."""

    name: str = ""
    value: float = 0.0
    units: str = ""
    sensor_class: str = ""


@dataclass
class NodeState:
    """
    Shared runtime state for the sensor node.

    Thread-safe container for state that multiple components need to access,
    such as the display pages and broadcast loop.
    """

    start_time: float = field(default_factory=time.time)
    broadcast_count: int = 0
    sensor_readings: list[SensorReadingInfo] = field(default_factory=list)
    ocr_result: str | None = None  # None = never run, str = result or "No result found"
    ocr_in_progress: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update_sensor_readings(
        self, readings: list[tuple[str, float, str, str]]
    ) -> None:
        """
        Update sensor readings (thread-safe).

        Args:
            readings: List of (name, value, units, sensor_class) tuples
        """
        with self._lock:
            self.sensor_readings = [
                SensorReadingInfo(name=n, value=v, units=u, sensor_class=c)
                for n, v, u, c in readings
            ]

    def get_sensor_readings(self) -> list[SensorReadingInfo]:
        """Get a copy of sensor readings (thread-safe)."""
        with self._lock:
            return [
                SensorReadingInfo(
                    name=r.name,
                    value=r.value,
                    units=r.units,
                    sensor_class=r.sensor_class,
                )
                for r in self.sensor_readings
            ]

    def increment_broadcast_count(self) -> None:
        """Increment broadcast counter (thread-safe)."""
        with self._lock:
            self.broadcast_count += 1

    def get_broadcast_count(self) -> int:
        """Get broadcast count (thread-safe)."""
        with self._lock:
            return self.broadcast_count

    def set_ocr_in_progress(self, in_progress: bool) -> None:
        """Set OCR in-progress flag (thread-safe)."""
        with self._lock:
            self.ocr_in_progress = in_progress

    def is_ocr_in_progress(self) -> bool:
        """Check if OCR is in progress (thread-safe)."""
        with self._lock:
            return self.ocr_in_progress

    def set_ocr_result(self, result: str | None) -> None:
        """Set OCR result (thread-safe)."""
        with self._lock:
            self.ocr_result = result

    def get_ocr_result(self) -> str | None:
        """Get OCR result (thread-safe)."""
        with self._lock:
            return self.ocr_result
