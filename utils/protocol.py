"""
Protocol definitions for sensor network communication.

This module provides message building, parsing, and CRC validation utilities
shared between outdoor nodes, gateways, and the Pi5 dashboard.

Message Types:
- LoRa broadcast: Outdoor node → Gateway (JSON with CRC)
- TCP discover: Pi5 ↔ Gateway (request/response)
- TCP data: Gateway → Pi5 (sensor readings stream)
"""

import json
import time
import zlib
from dataclasses import dataclass
from typing import Any


# =============================================================================
# Sensor ID Generation
# =============================================================================

def make_sensor_id(node_id: str, sensor_class: str, reading_name: str) -> str:
    """
    Generate a unique sensor ID.

    Format: {node_id}_{sensor_class}_{reading_name}
    All lowercase, spaces replaced with underscores.

    Args:
        node_id: Identifier for the node (e.g., "patio", "indoor-gateway")
        sensor_class: Name of the sensor class (e.g., "BME280")
        reading_name: Name of the specific reading (e.g., "temperature")

    Returns:
        Unique sensor ID string
    """
    parts = [node_id, sensor_class, reading_name]
    return "_".join(p.lower().replace(" ", "_").replace("-", "_") for p in parts)


def parse_sensor_id(sensor_id: str) -> tuple[str, str, str] | None:
    """
    Parse a sensor ID back into components.

    Args:
        sensor_id: The sensor ID to parse

    Returns:
        Tuple of (node_id, sensor_class, reading_name) or None if invalid
    """
    parts = sensor_id.split("_")
    if len(parts) >= 3:
        # First part is node_id, second is sensor_class, rest is reading_name
        return parts[0], parts[1], "_".join(parts[2:])
    return None


# =============================================================================
# CRC32 Utilities
# =============================================================================

def calculate_crc32(data: dict) -> str:
    """
    Calculate CRC32 checksum of a dictionary (excluding 'crc' field).

    Args:
        data: Dictionary to checksum (the 'crc' field is excluded if present)

    Returns:
        8-character lowercase hex string
    """
    # Remove crc field if present
    data_copy = {k: v for k, v in data.items() if k != "crc"}
    # Serialize to JSON with sorted keys for deterministic output
    json_str = json.dumps(data_copy, sort_keys=True, separators=(",", ":"))
    # Calculate CRC32 and format as hex
    crc = zlib.crc32(json_str.encode("utf-8")) & 0xFFFFFFFF
    return f"{crc:08x}"


def add_crc(data: dict) -> dict:
    """
    Add CRC32 checksum to a dictionary.

    Args:
        data: Dictionary to add CRC to

    Returns:
        New dictionary with 'crc' field added
    """
    result = dict(data)
    result["crc"] = calculate_crc32(data)
    return result


def verify_crc(data: dict) -> bool:
    """
    Verify CRC32 checksum of a dictionary.

    Args:
        data: Dictionary with 'crc' field

    Returns:
        True if CRC matches, False otherwise
    """
    if "crc" not in data:
        return False
    expected = data["crc"]
    actual = calculate_crc32(data)
    return expected == actual


# =============================================================================
# LoRa Broadcast Messages (Outdoor Node → Gateway)
# =============================================================================

@dataclass
class SensorReading:
    """A single sensor reading with metadata."""
    name: str
    units: str
    value: float | None
    sensor_class: str
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "units": self.units,
            "value": self.value,
            "sensor": self.sensor_class,
            "ts": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SensorReading":
        return cls(
            name=data["name"],
            units=data["units"],
            value=data["value"],
            sensor_class=data["sensor"],
            timestamp=data["ts"],
        )


def build_lora_message(node_id: str, readings: list[SensorReading]) -> bytes:
    """
    Build a LoRa broadcast message with CRC.

    Args:
        node_id: Identifier for this node
        readings: List of sensor readings

    Returns:
        UTF-8 encoded JSON bytes ready to transmit
    """
    message = {
        "node_id": node_id,
        "readings": [r.to_dict() for r in readings],
    }
    message = add_crc(message)
    return json.dumps(message, separators=(",", ":")).encode("utf-8")


def parse_lora_message(data: bytes) -> tuple[str, list[SensorReading]] | None:
    """
    Parse and validate a LoRa broadcast message.

    Args:
        data: Raw bytes received from LoRa

    Returns:
        Tuple of (node_id, readings) if valid, None if invalid/corrupted
    """
    try:
        message = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not verify_crc(message):
        return None

    try:
        node_id = message["node_id"]
        readings = [SensorReading.from_dict(r) for r in message["readings"]]
        return node_id, readings
    except (KeyError, TypeError):
        return None


# =============================================================================
# TCP Protocol Messages (Gateway ↔ Pi5)
# =============================================================================

# Message types
MSG_TYPE_DISCOVER = "discover"
MSG_TYPE_SENSORS = "sensors"
MSG_TYPE_SUBSCRIBE = "subscribe"
MSG_TYPE_DATA = "data"
MSG_TYPE_ERROR = "error"


@dataclass
class SensorInfo:
    """Sensor metadata for discovery response."""
    sensor_id: str
    node_id: str
    name: str
    units: str
    sensor_class: str
    is_local: bool

    def to_dict(self) -> dict:
        return {
            "id": self.sensor_id,
            "node_id": self.node_id,
            "name": self.name,
            "units": self.units,
            "sensor_class": self.sensor_class,
            "is_local": self.is_local,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SensorInfo":
        return cls(
            sensor_id=data["id"],
            node_id=data["node_id"],
            name=data["name"],
            units=data["units"],
            sensor_class=data["sensor_class"],
            is_local=data.get("is_local", False),
        )


@dataclass
class DataReading:
    """A sensor reading for the data stream."""
    sensor_id: str
    value: float | None
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "id": self.sensor_id,
            "value": self.value,
            "ts": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DataReading":
        return cls(
            sensor_id=data["id"],
            value=data["value"],
            timestamp=data["ts"],
        )


def build_tcp_message(msg_type: str, **kwargs) -> bytes:
    """
    Build a TCP protocol message.

    Args:
        msg_type: Message type (discover, sensors, subscribe, data, error)
        **kwargs: Message-specific fields

    Returns:
        Newline-terminated JSON bytes
    """
    message = {"type": msg_type, **kwargs}
    return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"


def parse_tcp_message(data: bytes) -> dict | None:
    """
    Parse a TCP protocol message.

    Args:
        data: Raw bytes (may include trailing newline)

    Returns:
        Parsed message dict, or None if invalid
    """
    try:
        return json.loads(data.strip().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# Convenience builders for specific message types

def build_discover_request() -> bytes:
    """Build a discover request message."""
    return build_tcp_message(MSG_TYPE_DISCOVER)


def build_sensors_response(gateway_id: str, sensors: list[SensorInfo]) -> bytes:
    """Build a sensors (discovery response) message."""
    return build_tcp_message(
        MSG_TYPE_SENSORS,
        gateway_id=gateway_id,
        sensors=[s.to_dict() for s in sensors],
    )


def build_subscribe_request() -> bytes:
    """Build a subscribe request message."""
    return build_tcp_message(MSG_TYPE_SUBSCRIBE)


def build_data_message(readings: list[DataReading]) -> bytes:
    """Build a data stream message."""
    return build_tcp_message(
        MSG_TYPE_DATA,
        readings=[r.to_dict() for r in readings],
    )


def build_error_message(error: str) -> bytes:
    """Build an error message."""
    return build_tcp_message(MSG_TYPE_ERROR, error=error)
