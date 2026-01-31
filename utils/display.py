"""
OLED display management with page cycling via microswitch.

Provides a modular system for displaying information on an SSD1306 OLED.
Pages can be cycled through using a connected microswitch.
"""

import socket
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime

from gpiozero import Button
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306

from .gateway_state import GatewayState


# =============================================================================
# Screen Pages
# =============================================================================


class ScreenPage(ABC):
    """
    Abstract base class for display pages.

    Each page provides up to 4 lines of text to display.
    To add a new page, subclass this and implement get_lines().
    """

    @abstractmethod
    def get_lines(self) -> list[str | None]:
        """
        Return up to 4 lines of text for the display.

        Returns:
            List of up to 4 strings. None means blank/skip line.
            If all lines are None, the screen will be turned off.
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
    - IP address
    - Uptime
    - Time since last packet
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

        return [
            f"IP: {ip}",
            f"Uptime: {uptime}",
            f"Last pkt: {last_pkt_str}",
            None,
        ]


class LastPacketPage(ScreenPage):
    """
    Last packet details page.

    Shows:
    - Header
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
        value_str = f"{last_pkt.sensor_value:.1f} {last_pkt.sensor_units}"

        return [
            "Last Packet",
            f"time: {time_str}",
            f"name: {name}",
            f"val: {value_str}",
        ]


# =============================================================================
# Screen Manager
# =============================================================================


class ScreenManager:
    """
    Manages the OLED display and page cycling.

    Handles:
    - SSD1306 display initialization and rendering
    - Microswitch input for page cycling
    - Periodic display refresh
    """

    def __init__(
        self,
        pages: list[ScreenPage],
        switch_pin: int = 16,
        i2c_port: int = 1,
        i2c_address: int = 0x3C,
        refresh_interval: float = 0.5,
    ):
        """
        Initialize the screen manager.

        Args:
            pages: List of ScreenPage instances to cycle through
            switch_pin: GPIO pin for the microswitch (BCM numbering)
            i2c_port: I2C bus number (usually 1 on Pi)
            i2c_address: I2C address of SSD1306 (usually 0x3C)
            refresh_interval: How often to refresh the display (seconds)
        """
        self._pages = pages
        self._current_page_idx = 0
        self._refresh_interval = refresh_interval
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Initialize display
        serial = i2c(port=i2c_port, address=i2c_address)
        self._device = ssd1306(serial)

        # Initialize switch
        self._switch = Button(switch_pin)
        self._switch.when_pressed = self._on_switch_pressed

    def _on_switch_pressed(self) -> None:
        """Handle switch press - cycle to next page."""
        with self._lock:
            self._current_page_idx = (self._current_page_idx + 1) % len(self._pages)
        self._refresh()

    def _refresh(self) -> None:
        """Refresh the display with current page content."""
        with self._lock:
            page = self._pages[self._current_page_idx]

        if page.is_off():
            self._device.hide()
            return

        self._device.show()
        lines = page.get_lines()

        with canvas(self._device) as draw:
            y = 0
            for line in lines:
                if line is not None:
                    draw.text((0, y), line, fill="white")
                y += 16  # 4 lines fit in 64 pixels

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
        self._device.clear()

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
        if self._switch:
            self._switch.close()


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
