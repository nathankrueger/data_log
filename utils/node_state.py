"""
Shared runtime state for the sensor node.

This module provides thread-safe state classes that can be shared
between node components (broadcast loop, display, OCR, commands, etc.).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from utils.radio_state import RadioState

if TYPE_CHECKING:
    from radio import RFM9xRadio
    from utils.led import RgbLed


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
    such as the display pages, broadcast loop, and command handlers.

    Required fields (must be provided at construction):
        node_id: This node's identifier string
        radio_state: RadioState encapsulating radio hardware and frequencies
        config_path: Path to config file for persistence

    Optional fields (have defaults):
        start_time, broadcast_count, sensor_readings, ocr_result, ocr_in_progress

    Backwards-compatible properties:
        radio, n2g_freq, g2n_freq delegate to radio_state
    """

    node_id: str
    radio_state: RadioState
    config_path: str
    start_time: float = field(default_factory=time.time)
    broadcast_count: int = 0
    sensor_readings: list[SensorReadingInfo] = field(default_factory=list)
    ocr_result: str | None = None  # None = never run, str = result or "No result found"
    ocr_in_progress: bool = False
    led: RgbLed | None = None
    default_brightness: int = 128
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ─── Backwards-Compatible Properties ────────────────────────────────────

    @property
    def radio(self) -> RFM9xRadio:
        """Get radio hardware instance (delegates to radio_state)."""
        return self.radio_state.radio

    @property
    def n2g_freq(self) -> float:
        """Get N2G frequency in MHz (delegates to radio_state)."""
        return self.radio_state.n2g_freq

    @n2g_freq.setter
    def n2g_freq(self, value: float) -> None:
        """Set N2G frequency in MHz (delegates to radio_state)."""
        self.radio_state.n2g_freq = value

    @property
    def g2n_freq(self) -> float:
        """Get G2N frequency in MHz (delegates to radio_state)."""
        return self.radio_state.g2n_freq

    @g2n_freq.setter
    def g2n_freq(self, value: float) -> None:
        """Set G2N frequency in MHz (delegates to radio_state)."""
        self.radio_state.g2n_freq = value

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
