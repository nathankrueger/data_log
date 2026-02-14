"""
Display package for OLED display management.

Provides a modular system for displaying information on OLED displays
with support for page cycling, scrolling, and autoscroll.
"""

from .base import (
    Display,
    OffPage,
    ScreenManager,
    ScreenPage,
    _format_duration,
    _get_ip_address,
)
from .ssd1306 import SSD1306Display

__all__ = [
    "Display",
    "ScreenPage",
    "ScreenManager",
    "SSD1306Display",
    "OffPage",
    "_get_ip_address",
    "_format_duration",
]
