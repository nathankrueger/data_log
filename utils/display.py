"""
OLED display management with page cycling.

Provides a modular system for displaying information on OLED displays.
Includes an abstract Display class for hardware abstraction and
SSD1306Display as the concrete implementation.
"""

import socket
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from urllib.parse import urlparse

from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306

from .gateway_state import GatewayState


# =============================================================================
# Display Hardware Abstraction
# =============================================================================


class Display(ABC):
    """Abstract base class for display hardware."""

    @property
    @abstractmethod
    def width(self) -> int:
        """Display width in pixels."""
        pass

    @property
    @abstractmethod
    def height(self) -> int:
        """Display height in pixels."""
        pass

    @property
    @abstractmethod
    def line_height(self) -> int:
        """Height of a single text line in pixels."""
        pass

    @property
    def max_lines(self) -> int:
        """Maximum visible lines based on height and line_height."""
        return self.height // self.line_height

    @abstractmethod
    def show(self) -> None:
        """Turn on/wake up the display."""
        pass

    @abstractmethod
    def hide(self) -> None:
        """Turn off/sleep the display."""
        pass

    @abstractmethod
    def clear(self) -> None:
        """Clear all pixels from the display."""
        pass

    @abstractmethod
    def render_lines(self, lines: list[str | None]) -> None:
        """Render lines of text to the display.

        Args:
            lines: List of text lines to display. None entries are blank lines.
                   Only the first `max_lines` will be shown.
        """
        pass


class SSD1306Display(Display):
    """SSD1306 OLED display implementation using luma.oled."""

    def __init__(self, i2c_port: int = 1, i2c_address: int = 0x3C):
        serial = i2c(port=i2c_port, address=i2c_address)
        self._device = ssd1306(serial)

    @property
    def width(self) -> int:
        return 128

    @property
    def height(self) -> int:
        return 64

    @property
    def line_height(self) -> int:
        return 16

    def show(self) -> None:
        self._device.show()

    def hide(self) -> None:
        self._device.hide()

    def clear(self) -> None:
        self._device.clear()

    def render_lines(self, lines: list[str | None]) -> None:
        with canvas(self._device) as draw:
            y = 0
            for line in lines[: self.max_lines]:
                if line is not None:
                    draw.text((0, y), line, fill="white")
                y += self.line_height


# =============================================================================
# Screen Pages
# =============================================================================


class ScreenPage(ABC):
    """
    Abstract base class for display pages.

    Each page provides lines of text to display. If more lines are returned
    than can fit on the display, ScreenManager handles scrolling.
    To add a new page, subclass this and implement get_lines().
    """

    @abstractmethod
    def get_lines(self) -> list[str | None]:
        """
        Return lines of text for the display.

        Returns:
            List of strings. None means blank/skip line.
            If all lines are None, the screen will be turned off.
            Can return any number of lines; ScreenManager handles scrolling.
        """
        pass

    def is_off(self) -> bool:
        """Return True if this page should turn the screen off."""
        return all(line is None for line in self.get_lines())


class OffPage(ScreenPage):
    """Page that turns the screen off."""

    def get_lines(self) -> list[str | None]:
        return [None, None, None, None]


class SystemInfoPage(ScreenPage):
    """
    System information page.

    Shows:
    - Header
    - IP address
    - Uptime
    - Time since last packet
    - Dashboard IP
    """

    def __init__(self, state: GatewayState):
        self._state = state

    def get_lines(self) -> list[str | None]:
        ip = _get_ip_address()
        uptime = _format_duration(time.time() - self._state.start_time)

        last_pkt = self._state.get_last_packet()
        if last_pkt.timestamp > 0:
            ago = _format_duration(time.time() - last_pkt.timestamp)
            last_pkt_str = f"{ago} ago"
        else:
            last_pkt_str = "Never"

        # Extract host from dashboard URL
        dashboard_ip = "N/A"
        if self._state.dashboard_url:
            parsed = urlparse(self._state.dashboard_url)
            dashboard_ip = parsed.hostname or "N/A"

        return [
            "System Information",
            f"IP: {ip}",
            f"Uptime: {uptime}",
            f"Last pkt: {last_pkt_str}",
            f"Dashbrd: {dashboard_ip}",
        ]


class LastPacketPage(ScreenPage):
    """
    Last packet details page.

    Shows:
    - Header [RSSI]
    - Timestamp
    - Sensor name
    - Sensor value
    """

    def __init__(self, state: GatewayState):
        self._state = state

    def get_lines(self) -> list[str | None]:
        last_pkt = self._state.get_last_packet()

        if last_pkt.timestamp == 0:
            return [
                "Last Packet",
                "---",
                "No packets yet",
                None,
            ]

        ts = datetime.fromtimestamp(last_pkt.timestamp)
        time_str = ts.strftime("%H:%M:%S")

        # Truncate long names to fit display
        name = last_pkt.sensor_name[:16]
        node = last_pkt.node_id[:16]
        rssi = last_pkt.rssi
        value_str = f"{last_pkt.sensor_value:.1f} {last_pkt.sensor_units}"

        return [
            f"Last Packet [RSSI: {rssi}]",
            f"time: {time_str}",
            f"name: {node}:{name}",
            f"val: {value_str}",
        ]


class GatewayLocalSensors(ScreenPage):
    """
    Gateway local sensors page.

    Shows:
    - Header
    - Up to 3 local sensor readings (name: value units)
    - Shows "---" for missing sensor slots
    """

    def __init__(self, state: GatewayState):
        self._state = state

    def get_lines(self) -> list[str | None]:
        sensors = self._state.get_local_sensors()

        lines: list[str | None] = ["Local Sensors"]

        # Show up to 3 sensors (we have 4 lines, 1 for header)
        for i in range(3):
            if i < len(sensors):
                s = sensors[i]
                # Truncate name to fit, leave room for value
                name = s.name[:8]
                lines.append(f"{name}: {s.value:.1f} {s.units}")
            else:
                lines.append("---")

        return lines


# =============================================================================
# Screen Manager
# =============================================================================


class ScreenManager:
    """
    Manages display pages and scrolling.

    Handles:
    - Page management and cycling
    - Line scrolling within pages
    - Periodic display refresh

    GPIO button handling is external; use advance_page() and scroll_page()
    methods to wire up buttons.
    """

    def __init__(
        self,
        display: Display,
        pages: list[ScreenPage],
        refresh_interval: float = 0.5,
    ):
        """
        Initialize the screen manager.

        Args:
            display: Display instance to render to
            pages: List of ScreenPage instances to cycle through
            refresh_interval: How often to refresh the display (seconds)
        """
        self._display = display
        self._pages = pages
        self._current_page_idx = 0
        self._line_offset = 0
        self._refresh_interval = refresh_interval
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def advance_page(self) -> None:
        """Advance to the next page, wrapping to first. Resets scroll offset."""
        with self._lock:
            self._current_page_idx = (self._current_page_idx + 1) % len(self._pages)
            self._line_offset = 0
        self._refresh()

    def set_page(self, index: int) -> None:
        """Set the current page by index (thread-safe). Resets scroll offset."""
        with self._lock:
            if 0 <= index < len(self._pages):
                self._current_page_idx = index
                self._line_offset = 0
        self._refresh()

    def scroll_page(self, delta: int = 1) -> None:
        """Scroll the visible window by delta lines (positive = down).

        Wraps to top when reaching the bottom.
        """
        with self._lock:
            page = self._pages[self._current_page_idx]
            lines = page.get_lines()
            total_lines = len(lines)
            max_lines = self._display.max_lines

            if total_lines <= max_lines:
                # No scrolling needed, content fits
                return

            self._line_offset += delta
            # Wrap to top when we've scrolled past the end
            if self._line_offset + max_lines > total_lines:
                self._line_offset = 0
            elif self._line_offset < 0:
                self._line_offset = max(0, total_lines - max_lines)
        self._refresh()

    def _refresh(self) -> None:
        """Refresh the display with current page content."""
        with self._lock:
            page = self._pages[self._current_page_idx]
            offset = self._line_offset

        if page.is_off():
            self._display.hide()
            return

        self._display.show()
        lines = page.get_lines()

        # Slice the visible window
        max_lines = self._display.max_lines
        visible_lines = lines[offset : offset + max_lines]

        self._display.render_lines(visible_lines)

    def start(self) -> None:
        """Start the display refresh thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the display refresh thread and clear display."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        self._display.clear()

    def _run(self) -> None:
        """Background thread that periodically refreshes the display."""
        while self._running:
            try:
                self._refresh()
            except Exception:
                pass  # Don't crash on display errors
            time.sleep(self._refresh_interval)

    def close(self) -> None:
        """Clean up resources."""
        self.stop()


# =============================================================================
# Utility Functions
# =============================================================================


def _get_ip_address() -> str:
    """Get the primary IP address of this machine."""
    try:
        # Connect to a remote address to determine local IP
        # (doesn't actually send data)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "Unknown"


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    seconds = int(seconds)

    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m = seconds // 60
        s = seconds % 60
        return f"{m}m{s}s"
    else:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h{m}m"
