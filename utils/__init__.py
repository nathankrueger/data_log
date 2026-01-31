"""
Utility modules for data_log.

This package provides shared utilities for the sensor network.
"""

from .display import (
    LastPacketPage,
    OffPage,
    ScreenManager,
    ScreenPage,
    SystemInfoPage,
)
from .gateway_state import GatewayState, LastPacketInfo
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
    build_lora_message,
    parse_lora_message,
    build_lora_packets,
    parse_lora_packet,
    # TCP protocol
    MSG_TYPE_DISCOVER,
    MSG_TYPE_SENSORS,
    MSG_TYPE_SUBSCRIBE,
    MSG_TYPE_DATA,
    MSG_TYPE_ERROR,
    SensorInfo,
    DataReading,
    build_tcp_message,
    parse_tcp_message,
    build_discover_request,
    build_sensors_response,
    build_subscribe_request,
    build_data_message,
    build_error_message,
)

__all__ = [
    # Gateway state
    "GatewayState",
    "LastPacketInfo",
    # Display
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
    "build_lora_message",
    "parse_lora_message",
    "build_lora_packets",
    "parse_lora_packet",
    # TCP protocol
    "MSG_TYPE_DISCOVER",
    "MSG_TYPE_SENSORS",
    "MSG_TYPE_SUBSCRIBE",
    "MSG_TYPE_DATA",
    "MSG_TYPE_ERROR",
    "SensorInfo",
    "DataReading",
    "build_tcp_message",
    "parse_tcp_message",
    "build_discover_request",
    "build_sensors_response",
    "build_subscribe_request",
    "build_data_message",
    "build_error_message",
]
