"""
Display pages for sensor node.

Provides ScreenPage implementations specific to sensor nodes:
- SensorValuesPage: Shows all sensor readings with autoscroll
- NodeInfoPage: Shows node system information
- ArducamOCRPage: Shows OCR results from camera capture
"""

import logging
import threading
import time
from pathlib import Path

from display.base import ScreenPage, _format_duration, _get_ip_address
from utils.node_state import NodeState

logger = logging.getLogger(__name__)


class SensorValuesPage(ScreenPage):
    """
    Displays all sensor readings with autoscroll or manual circular scroll.

    Shows one reading per line: "name: value units"
    When auto_scroll=True, autoscrolls through all sensors at 2s intervals.
    When auto_scroll=False, use scroll() to cycle readings in round-robin fashion.
    """

    def __init__(self, state: NodeState, auto_scroll: bool = False):
        self._state = state
        self._auto_scroll = auto_scroll
        self._scroll_offset = 0

    def get_lines(self) -> list[str | None]:
        readings = self._state.get_sensor_readings()

        if not readings:
            return ["Sensor Values", "---", "No readings yet", None]

        sensor_lines: list[str] = []
        for r in readings:
            # Format: "temp: 72.5 F" - truncate name if needed
            name = r.name[:8]
            sensor_lines.append(f"{name}: {r.value:.1f} {r.units}")

        # Circular rotation for manual scroll mode
        if not self._auto_scroll and sensor_lines:
            offset = self._scroll_offset % len(sensor_lines)
            sensor_lines = sensor_lines[offset:] + sensor_lines[:offset]

        return ["Sensor Values"] + sensor_lines

    def get_autoscroll_interval(self) -> float | None:
        return 2.0 if self._auto_scroll else None

    def scroll(self) -> None:
        """Advance circular scroll by one reading."""
        self._scroll_offset += 1


class NodeInfoPage(ScreenPage):
    """
    Node system information page.

    Shows IP, uptime, broadcast count.
    """

    def __init__(self, state: NodeState):
        self._state = state

    def get_lines(self) -> list[str | None]:
        ip = _get_ip_address()
        uptime = _format_duration(time.time() - self._state.start_time)
        broadcasts = self._state.get_broadcast_count()

        return [
            f"Node Info - {self._state.node_id}",
            f"IP: {ip}",
            f"Uptime: {uptime}",
            f"Broadcasts: {broadcasts}",
        ]


class ArducamOCRPage(ScreenPage):
    """
    Arducam OCR result page.

    Shows last OCR result or "No result found".
    The do_action() method triggers capture+OCR when the action button is pressed.
    """

    def __init__(self, state: NodeState):
        self._state = state
        # Import at construction time to avoid delay on first button press
        from sensors.arducam import CropMode, capture_and_ocr
        self._capture_and_ocr = capture_and_ocr
        self._crop_mode = CropMode

    def get_lines(self) -> list[str | None]:
        if self._state.is_ocr_in_progress():
            return [
                "Arducam OCR",
                "---",
                "Capturing...",
                None,
            ]

        result = self._state.get_ocr_result()

        if result is None:
            return [
                "Arducam OCR",
                "---",
                "Press to capture",
                None,
            ]

        return [
            "Arducam OCR",
            "---",
            f"Result: {result}",
            None,
        ]

    def do_action(self) -> None:
        """Trigger OCR capture in background thread."""
        if self._state.is_ocr_in_progress():
            return  # Already running

        # Set immediately so display updates on next refresh
        self._state.set_ocr_in_progress(True)

        def _do_capture():
            try:
                result = self._capture_and_ocr(
                    output_dir=Path.home() / "Pictures",
                    crop_mode=self._crop_mode.NONE,
                    preprocess=False
                )
                self._state.set_ocr_result(result if result else "No result found")
            except Exception as e:
                logger.error(f"OCR capture failed: {e}")
                self._state.set_ocr_result("Capture failed")
            finally:
                self._state.set_ocr_in_progress(False)

        thread = threading.Thread(target=_do_capture, daemon=True)
        thread.start()
