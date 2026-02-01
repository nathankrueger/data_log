"""
Utility modules for data_log.

This package provides shared utilities for the sensor network.
"""

from .display import (
    GatewayLocalSensors,
    LastPacketPage,
    OffPage,
    ScreenManager,
    ScreenPage,
    SystemInfoPage,
)
from .gateway_state import GatewayState, LastPacketInfo, LocalSensorReading
from .led import RgbLed
from .protocol import (
    # Sensor ID utilities
    make_sensor_id,
    parse_sensor_id,
    # CRC utilities
    calculate_crc32,
    add_crc,
    verify_crc,
    # LoRa messages
    SensorReading,
    build_lora_packets,
    parse_lora_packet,
)

__all__ = [
    # Gateway state
    "GatewayState",
    "LastPacketInfo",
    "LocalSensorReading",
    # Display
    "GatewayLocalSensors",
    "LastPacketPage",
    "OffPage",
    "ScreenManager",
    "ScreenPage",
    "SystemInfoPage",
    # LED
    "RgbLed",
    # Sensor ID utilities
    "make_sensor_id",
    "parse_sensor_id",
    # CRC utilities
    "calculate_crc32",
    "add_crc",
    "verify_crc",
    # LoRa messages
    "SensorReading",
    "build_lora_packets",
    "parse_lora_packet",
]
