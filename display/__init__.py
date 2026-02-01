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
    SSD1306Display,
    _format_duration,
    _get_ip_address,
)
from .gateway import GatewayLocalSensors, LastPacketPage, SystemInfoPage
from .node import ArducamOCRPage, NodeInfoPage, SensorValuesPage

__all__ = [
    # Base classes
    "Display",
    "ScreenPage",
    "ScreenManager",
    "SSD1306Display",
    "OffPage",
    # Gateway pages
    "SystemInfoPage",
    "LastPacketPage",
    "GatewayLocalSensors",
    # Node pages
    "SensorValuesPage",
    "NodeInfoPage",
    "ArducamOCRPage",
    # Utilities
    "_get_ip_address",
    "_format_duration",
]
