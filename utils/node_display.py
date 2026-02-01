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

from .display import ScreenPage, _format_duration, _get_ip_address
from .node_state import NodeState

logger = logging.getLogger(__name__)


class SensorValuesPage(ScreenPage):
    """
    Displays all sensor readings with autoscroll.

    Shows one reading per line: "name: value units"
    Autoscrolls through all sensors at 2s intervals.
    """

    def __init__(self, state: NodeState):
        self._state = state

    def get_lines(self) -> list[str | None]:
        readings = self._state.get_sensor_readings()

        if not readings:
            return ["Sensor Values", "---", "No readings yet", None]

        lines: list[str | None] = ["Sensor Values"]
        for r in readings:
            # Format: "temp: 72.5 F" - truncate name if needed
            name = r.name[:8]
            lines.append(f"{name}: {r.value:.1f} {r.units}")

        return lines

    def get_autoscroll_interval(self) -> float | None:
        return 2.0  # 2 second autoscroll


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
            "Node Info",
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

        def _do_capture():
            self._state.set_ocr_in_progress(True)
            try:
                from sensors.arducam import capture_and_ocr

                result = capture_and_ocr()
                self._state.set_ocr_result(result if result else "No result found")
            except Exception as e:
                logger.error(f"OCR capture failed: {e}")
                self._state.set_ocr_result("Capture failed")
            finally:
                self._state.set_ocr_in_progress(False)

        thread = threading.Thread(target=_do_capture, daemon=True)
        thread.start()
