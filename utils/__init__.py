"""
Utility modules for data_log.

This package provides shared utilities for the sensor network.
"""

from .gateway_state import GatewayState, LastPacketInfo, LocalSensorReading
from .led import RgbLed
from .node_state import NodeState, SensorReadingInfo
from .protocol import (
    SensorReading,
    add_crc,
    build_lora_packets,
    calculate_crc32,
    make_sensor_id,
    parse_lora_packet,
    parse_sensor_id,
    verify_crc,
)

__all__ = [
    # Gateway state
    "GatewayState",
    "LastPacketInfo",
    "LocalSensorReading",
    # Node state
    "NodeState",
    "SensorReadingInfo",
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
