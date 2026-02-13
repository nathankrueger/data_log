"""
Protocol definitions for sensor network communication.

This module provides message building, parsing, and CRC validation utilities
shared between outdoor nodes and gateways.

Message Types:
- LoRa broadcast: Outdoor node → Gateway (JSON with CRC)
- LoRa command: Gateway → Node (JSON with CRC)
"""

import json
import logging
import time
import zlib
from dataclasses import dataclass
from typing import Any

# Logger for command/ACK debugging (enabled via --cmd-debug)
cmd_logger = logging.getLogger("cmd_debug")


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

def calculate_crc32(data: dict, crc_key: str = "crc") -> str:
    """
    Calculate CRC32 checksum of a dictionary (excluding CRC field).

    Args:
        data: Dictionary to checksum (the CRC field is excluded if present)
        crc_key: The key name used for CRC field (default: "crc")

    Returns:
        8-character lowercase hex string
    """
    # Remove crc field if present
    data_copy = {k: v for k, v in data.items() if k != crc_key}
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


def verify_crc(data: dict, crc_key: str = "crc") -> bool:
    """
    Verify CRC32 checksum of a dictionary.

    Args:
        data: Dictionary with CRC field
        crc_key: The key name used for CRC field (default: "crc")

    Returns:
        True if CRC matches, False otherwise
    """
    if crc_key not in data:
        return False
    expected = data[crc_key]
    actual = calculate_crc32(data, crc_key)
    if expected != actual:
        # Debug: show CRC input string for mismatch analysis
        data_copy = {k: v for k, v in data.items() if k != crc_key}
        crc_input = json.dumps(data_copy, sort_keys=True, separators=(",", ":"))
        cmd_logger.debug(
            "CRC_MISMATCH expected=%s actual=%s crc_input=%r",
            expected, actual, crc_input
        )
    return expected == actual


# =============================================================================
# LoRa Broadcast Messages (Outdoor Node → Gateway)
# =============================================================================

# Max LoRa payload size (conservative to avoid issues)
LORA_MAX_PAYLOAD = 250

@dataclass
class SensorReading:
    """A single sensor reading with metadata."""
    name: str
    units: str
    value: float | None
    sensor_class: str
    timestamp: float
    precision: int = 3  # Number of decimal places for float values

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


def build_lora_packets(node_id: str, readings: list[SensorReading]) -> list[bytes]:
    """
    Build compact LoRa packets from readings, splitting if needed.

    Uses deterministic sensor class IDs from sensors.SENSOR_CLASS_IDS registry.
    Packs as many readings as possible into each packet while staying
    under the LoRa payload limit.

    Compact format keys:
        n = node_id
        t = timestamp
        r = readings array
        s = sensor class ID (from registry)
        k = reading name (key)
        u = units
        v = value
        c = CRC

    Args:
        node_id: Identifier for this node
        readings: List of sensor readings (all should share same timestamp)

    Returns:
        List of UTF-8 encoded JSON packets ready to transmit
    """
    from sensors import get_sensor_class_id

    if not readings:
        return []

    # Round timestamp to 3 decimal places (millisecond precision)
    timestamp = round(readings[0].timestamp, 3)

    def build_packet(ts: float, compact_readings: list[dict]) -> bytes:
        message = {"n": node_id, "t": ts, "r": compact_readings}
        message["c"] = calculate_crc32(message)
        return json.dumps(message, separators=(",", ":")).encode("utf-8")

    packets = []
    current_readings = []

    for reading in readings:
        sensor_id = get_sensor_class_id(reading.sensor_class)
        if sensor_id is None:
            # Unknown sensor class, skip or use -1
            sensor_id = -1

        # Round value to specified precision
        value = reading.value
        if value is not None:
            value = round(value, reading.precision)

        compact = {
            "s": sensor_id,
            "k": reading.name,
            "u": reading.units,
            "v": value,
        }

        # Try adding to current batch
        test_readings = current_readings + [compact]
        test_packet = build_packet(timestamp, test_readings)

        if len(test_packet) <= LORA_MAX_PAYLOAD:
            current_readings.append(compact)
        else:
            # Current batch is full, emit it and start new batch
            if current_readings:
                packets.append(build_packet(timestamp, current_readings))
            current_readings = [compact]

    # Emit remaining readings
    if current_readings:
        packets.append(build_packet(timestamp, current_readings))

    return packets


def parse_lora_packet(data: bytes) -> tuple[str, list[SensorReading]] | None:
    """
    Parse a compact LoRa packet.

    Uses deterministic sensor class IDs from sensors.SENSOR_ID_CLASSES registry.

    Args:
        data: Raw bytes received from LoRa

    Returns:
        Tuple of (node_id, readings) if valid, None if invalid/corrupted
    """
    from sensors import get_sensor_class_name

    try:
        message = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not verify_crc(message, crc_key="c"):
        return None

    try:
        node_id = message["n"]
        timestamp = message["t"]

        readings = []
        for r in message["r"]:
            sensor_class = get_sensor_class_name(r["s"])
            if sensor_class is None:
                sensor_class = f"unknown_{r['s']}"

            readings.append(SensorReading(
                name=r["k"],
                units=r["u"],
                value=r["v"],
                sensor_class=sensor_class,
                timestamp=timestamp,
            ))

        return node_id, readings

    except (KeyError, TypeError, ValueError):
        return None


# =============================================================================
# LoRa Command Messages (Gateway → Node)
# =============================================================================

@dataclass
class CommandPacket:
    """A command to be sent to nodes."""
    command: str
    args: list[str]
    node_id: str  # Empty string for broadcast
    timestamp: int
    crc: str

    def is_broadcast(self) -> bool:
        """Return True if this is a broadcast command (no specific target)."""
        return self.node_id == ""

    def get_command_id(self) -> str:
        """Get unique command ID for ACK matching."""
        return f"{self.timestamp}_{self.crc[:4]}"


@dataclass
class AckPacket:
    """An acknowledgment for a received command."""
    command_id: str
    node_id: str
    payload: dict | None = None  # Optional response data from node


def build_command_packet(
    command: str, args: list[str], node_id: str = ""
) -> tuple[bytes, str]:
    """
    Build a LoRa command packet with CRC.

    Compact format keys:
        t = "cmd" (message type)
        n = node_id (empty string for broadcast)
        cmd = command name
        a = args list
        ts = timestamp
        c = CRC

    Args:
        command: Command name (e.g., "reboot", "set_interval")
        args: List of string arguments
        node_id: Target node ID, or empty string for broadcast

    Returns:
        Tuple of (packet_bytes, command_id) where command_id is for ACK matching
    """
    timestamp = int(time.time())
    message: dict[str, Any] = {
        "t": "cmd",
        "n": node_id,
        "cmd": command,
        "a": args,
        "ts": timestamp,
    }
    crc = calculate_crc32(message)
    message["c"] = crc
    command_id = f"{timestamp}_{crc[:4]}"
    packet = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return packet, command_id


def parse_command_packet(data: bytes) -> CommandPacket | None:
    """
    Parse and verify a LoRa command packet.

    Args:
        data: Raw bytes received from LoRa

    Returns:
        CommandPacket if valid, None if invalid/corrupted
    """
    try:
        message = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    # Check message type
    if message.get("t") != "cmd":
        return None

    # Verify CRC
    if not verify_crc(message, crc_key="c"):
        return None

    try:
        return CommandPacket(
            command=message["cmd"],
            args=message["a"],
            node_id=message["n"],
            timestamp=message["ts"],
            crc=message["c"],
        )
    except (KeyError, TypeError, ValueError):
        return None


# =============================================================================
# LoRa ACK Messages (Node → Gateway)
# =============================================================================


def build_ack_packet(
    command_id: str, node_id: str, payload: dict | None = None
) -> bytes:
    """
    Build an ACK packet for a received command.

    Compact format keys:
        t = "ack" (message type)
        id = command_id (timestamp_crcprefix)
        n = node_id sending the ACK
        p = optional response payload dict
        c = CRC

    Args:
        command_id: ID of the command being acknowledged
        node_id: ID of the node sending the ACK
        payload: Optional response data to include in ACK

    Returns:
        UTF-8 encoded JSON packet ready to transmit
    """
    message: dict[str, Any] = {
        "t": "ack",
        "id": command_id,
        "n": node_id,
    }
    if payload is not None:
        message["p"] = payload
    message["c"] = calculate_crc32(message)
    return json.dumps(message, separators=(",", ":")).encode("utf-8")


def parse_ack_packet(data: bytes) -> AckPacket | None:
    """
    Parse and verify a LoRa ACK packet.

    Args:
        data: Raw bytes received from LoRa

    Returns:
        AckPacket if valid, None if invalid/corrupted or not an ACK
    """
    try:
        message = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger = logging.getLogger(__name__)
        logger.warning("ACK_JSON_FAIL len=%d error=%s data=%r", len(data), e, data[:100])
        return None

    # Check message type
    msg_type = message.get("t")
    if msg_type != "ack":
        # Only warn if this looks like an ACK (has "id" field) but wrong type
        if "id" in message:
            logger = logging.getLogger(__name__)
            logger.warning("ACK_TYPE_FAIL got=%s expected=ack keys=%s", msg_type, list(message.keys()))
        return None

    # Verify CRC
    if not verify_crc(message, crc_key="c"):
        expected = message.get("c", "missing")
        actual = calculate_crc32(message, crc_key="c")
        logger = logging.getLogger(__name__)
        logger.warning(
            "ACK_CRC_FAIL expected=%s actual=%s id=%s node=%s",
            expected, actual, message.get("id"), message.get("n"),
        )
        return None

    try:
        return AckPacket(
            command_id=message["id"],
            node_id=message["n"],
            payload=message.get("p"),  # Optional response payload
        )
    except (KeyError, TypeError, ValueError) as e:
        cmd_logger.debug("ACK_FIELD_ERR error=%s message=%s", e, message)
        return None
