"""Tests for LoRa protocol packing and unpacking."""

import json
import time

import pytest

from sensors import SENSOR_CLASS_IDS, get_sensor_class_id, get_sensor_class_name
from utils.protocol import (
    AckPacket,
    CommandPacket,
    LORA_MAX_PAYLOAD,
    SensorReading,
    build_ack_packet,
    build_command_packet,
    build_lora_packets,
    calculate_crc32,
    parse_ack_packet,
    parse_command_packet,
    parse_lora_packet,
    verify_crc,
)
from utils.command_registry import CommandRegistry, CommandScope
from gateway_server import CommandQueue


def make_reading(name: str, units: str, value: float, sensor_class: str) -> SensorReading:
    """Helper to create a SensorReading with current timestamp."""
    return SensorReading(
        name=name,
        units=units,
        value=value,
        sensor_class=sensor_class,
        timestamp=time.time(),
    )


class TestCRC:
    """Tests for CRC calculation and verification."""

    def test_crc_deterministic(self):
        """Same data should produce same CRC."""
        data = {"a": 1, "b": "test"}
        assert calculate_crc32(data) == calculate_crc32(data)

    def test_crc_different_for_different_data(self):
        """Different data should produce different CRC."""
        data1 = {"a": 1}
        data2 = {"a": 2}
        assert calculate_crc32(data1) != calculate_crc32(data2)

    def test_verify_crc_valid(self):
        """verify_crc should return True for valid CRC."""
        data = {"a": 1, "b": "test"}
        data["crc"] = calculate_crc32(data)
        assert verify_crc(data) is True

    def test_verify_crc_invalid(self):
        """verify_crc should return False for tampered data."""
        data = {"a": 1, "b": "test"}
        data["crc"] = calculate_crc32(data)
        data["a"] = 999  # Tamper with data
        assert verify_crc(data) is False

    def test_verify_crc_missing(self):
        """verify_crc should return False when CRC is missing."""
        data = {"a": 1, "b": "test"}
        assert verify_crc(data) is False


class TestBuildLoraPackets:
    """Tests for build_lora_packets function."""

    def test_single_reading_fits_in_one_packet(self):
        """A single reading should produce one packet."""
        readings = [make_reading("Temperature", "F", 72.5, "BME280TempPressureHumidity")]
        packets = build_lora_packets("test-node", readings)

        assert len(packets) == 1
        assert len(packets[0]) <= LORA_MAX_PAYLOAD

    def test_packet_contains_expected_fields(self):
        """Packet should contain node, timestamp, readings, and CRC."""
        readings = [make_reading("Temperature", "F", 72.5, "BME280TempPressureHumidity")]
        packets = build_lora_packets("test-node", readings)

        data = json.loads(packets[0].decode("utf-8"))
        assert data["n"] == "test-node"
        assert "t" in data
        assert "r" in data
        assert "c" in data
        assert len(data["r"]) == 1

    def test_reading_uses_sensor_class_id(self):
        """Reading should use integer sensor class ID from registry."""
        readings = [make_reading("Temperature", "F", 72.5, "BME280TempPressureHumidity")]
        packets = build_lora_packets("test-node", readings)

        data = json.loads(packets[0].decode("utf-8"))
        reading = data["r"][0]
        assert reading["s"] == SENSOR_CLASS_IDS["BME280TempPressureHumidity"]
        assert reading["k"] == "Temperature"
        assert reading["u"] == "F"
        assert reading["v"] == 72.5

    def test_multiple_readings_same_packet(self):
        """Multiple small readings should fit in one packet."""
        readings = [
            make_reading("Temperature", "F", 72.5, "BME280TempPressureHumidity"),
            make_reading("Pressure", "hPa", 1013.25, "BME280TempPressureHumidity"),
            make_reading("Humidity", "%", 45.0, "BME280TempPressureHumidity"),
        ]
        packets = build_lora_packets("test-node", readings)

        assert len(packets) == 1
        data = json.loads(packets[0].decode("utf-8"))
        assert len(data["r"]) == 3

    def test_splits_when_exceeds_payload(self):
        """Should split into multiple packets when exceeding payload limit."""
        # Create many readings to exceed payload
        readings = [
            make_reading(f"Sensor{i}", "units", float(i), "BME280TempPressureHumidity")
            for i in range(20)
        ]
        packets = build_lora_packets("test-node", readings)

        assert len(packets) > 1
        for packet in packets:
            assert len(packet) <= LORA_MAX_PAYLOAD

        # Verify all readings are present across packets
        total_readings = sum(len(json.loads(p.decode())["r"]) for p in packets)
        assert total_readings == 20

    def test_empty_readings_returns_empty(self):
        """Empty readings list should return empty packets list."""
        packets = build_lora_packets("test-node", [])
        assert packets == []

    def test_unknown_sensor_class_uses_negative_id(self):
        """Unknown sensor class should use -1 as ID."""
        readings = [make_reading("Test", "x", 1.0, "UnknownSensorClass")]
        packets = build_lora_packets("test-node", readings)

        data = json.loads(packets[0].decode("utf-8"))
        assert data["r"][0]["s"] == -1


class TestParseLoraPacket:
    """Tests for parse_lora_packet function."""

    def test_roundtrip_single_reading(self):
        """Build then parse should return equivalent data."""
        original = [make_reading("Temperature", "F", 72.5, "BME280TempPressureHumidity")]
        packets = build_lora_packets("test-node", original)

        result = parse_lora_packet(packets[0])
        assert result is not None

        node_id, readings = result
        assert node_id == "test-node"
        assert len(readings) == 1
        assert readings[0].name == "Temperature"
        assert readings[0].units == "F"
        assert readings[0].value == 72.5
        assert readings[0].sensor_class == "BME280TempPressureHumidity"

    def test_roundtrip_multiple_readings(self):
        """Build then parse should preserve all readings."""
        original = [
            make_reading("Temperature", "F", 72.5, "BME280TempPressureHumidity"),
            make_reading("Accel X", "g", 0.01, "MMA8452Accelerometer"),
        ]
        packets = build_lora_packets("test-node", original)
        result = parse_lora_packet(packets[0])

        assert result is not None
        node_id, readings = result
        assert len(readings) == 2
        assert readings[0].sensor_class == "BME280TempPressureHumidity"
        assert readings[1].sensor_class == "MMA8452Accelerometer"

    def test_roundtrip_split_packets(self):
        """All split packets should parse correctly."""
        original = [
            make_reading(f"Sensor{i}", "units", float(i), "BME280TempPressureHumidity")
            for i in range(20)
        ]
        packets = build_lora_packets("test-node", original)

        all_readings = []
        for packet in packets:
            result = parse_lora_packet(packet)
            assert result is not None
            _, readings = result
            all_readings.extend(readings)

        assert len(all_readings) == 20

    def test_parse_invalid_json(self):
        """Invalid JSON should return None."""
        assert parse_lora_packet(b"not json") is None

    def test_parse_invalid_crc(self):
        """Tampered packet should return None."""
        readings = [make_reading("Test", "x", 1.0, "BME280TempPressureHumidity")]
        packets = build_lora_packets("test-node", readings)

        # Tamper with the packet
        data = json.loads(packets[0].decode("utf-8"))
        data["n"] = "tampered"
        tampered = json.dumps(data).encode("utf-8")

        assert parse_lora_packet(tampered) is None

    def test_parse_missing_fields(self):
        """Packet missing required fields should return None."""
        incomplete = json.dumps({"n": "test"}).encode("utf-8")
        assert parse_lora_packet(incomplete) is None

    def test_unknown_sensor_id_handled(self):
        """Unknown sensor class ID should result in 'unknown_X' class name."""
        # Manually create a packet with unknown sensor ID
        data = {
            "n": "test-node",
            "t": time.time(),
            "r": [{"s": 999, "k": "Test", "u": "x", "v": 1.0}],
        }
        data["c"] = calculate_crc32(data)
        packet = json.dumps(data).encode("utf-8")

        result = parse_lora_packet(packet)
        assert result is not None
        _, readings = result
        assert readings[0].sensor_class == "unknown_999"


class TestPrecision:
    """Tests for float precision handling in LoRa packets."""

    def test_default_precision_is_3(self):
        """Default precision should round to 3 decimal places."""
        reading = SensorReading(
            name="Temperature",
            units="F",
            value=72.123456789,
            sensor_class="BME280TempPressureHumidity",
            timestamp=time.time(),
        )
        packets = build_lora_packets("test-node", [reading])

        data = json.loads(packets[0].decode("utf-8"))
        assert data["r"][0]["v"] == 72.123

    def test_custom_precision(self):
        """Custom precision should be respected."""
        reading = SensorReading(
            name="Temperature",
            units="F",
            value=72.123456789,
            sensor_class="BME280TempPressureHumidity",
            timestamp=time.time(),
            precision=1,
        )
        packets = build_lora_packets("test-node", [reading])

        data = json.loads(packets[0].decode("utf-8"))
        assert data["r"][0]["v"] == 72.1

    def test_precision_zero(self):
        """Precision of 0 should round to integer."""
        reading = SensorReading(
            name="Temperature",
            units="F",
            value=72.6,
            sensor_class="BME280TempPressureHumidity",
            timestamp=time.time(),
            precision=0,
        )
        packets = build_lora_packets("test-node", [reading])

        data = json.loads(packets[0].decode("utf-8"))
        assert data["r"][0]["v"] == 73

    def test_precision_with_none_value(self):
        """None values should pass through unchanged."""
        reading = SensorReading(
            name="Temperature",
            units="F",
            value=None,
            sensor_class="BME280TempPressureHumidity",
            timestamp=time.time(),
            precision=3,
        )
        packets = build_lora_packets("test-node", [reading])

        data = json.loads(packets[0].decode("utf-8"))
        assert data["r"][0]["v"] is None

    def test_mixed_precision_readings(self):
        """Multiple readings with different precisions should each be handled correctly."""
        readings = [
            SensorReading(
                name="Temperature",
                units="F",
                value=72.123456,
                sensor_class="BME280TempPressureHumidity",
                timestamp=time.time(),
                precision=2,
            ),
            SensorReading(
                name="Pressure",
                units="hPa",
                value=1013.256789,
                sensor_class="BME280TempPressureHumidity",
                timestamp=time.time(),
                precision=1,
            ),
        ]
        packets = build_lora_packets("test-node", readings)

        data = json.loads(packets[0].decode("utf-8"))
        assert data["r"][0]["v"] == 72.12
        assert data["r"][1]["v"] == 1013.3

    def test_precision_roundtrip(self):
        """Precision should be applied during build, but roundtrip should work."""
        original = SensorReading(
            name="Temperature",
            units="F",
            value=72.123456789,
            sensor_class="BME280TempPressureHumidity",
            timestamp=time.time(),
            precision=3,
        )
        packets = build_lora_packets("test-node", [original])

        result = parse_lora_packet(packets[0])
        assert result is not None

        node_id, readings = result
        # Value should be rounded to 3 decimal places
        assert readings[0].value == 72.123


class TestSensorRegistry:
    """Tests for sensor class ID registry."""

    def test_id_and_name_are_inverses(self):
        """get_sensor_class_id and get_sensor_class_name should be inverses."""
        for class_name, class_id in SENSOR_CLASS_IDS.items():
            assert get_sensor_class_id(class_name) == class_id
            assert get_sensor_class_name(class_id) == class_name

    def test_unknown_class_returns_none(self):
        """Unknown sensor class should return None."""
        assert get_sensor_class_id("NonexistentSensor") is None
        assert get_sensor_class_name(9999) is None

    def test_ids_are_alphabetically_ordered(self):
        """Sensor IDs should be assigned in alphabetical order of class names."""
        class_names = list(SENSOR_CLASS_IDS.keys())
        assert class_names == sorted(class_names)


# =============================================================================
# Command Packet Tests
# =============================================================================


class TestBuildCommandPacket:
    """Tests for build_command_packet function."""

    def test_builds_valid_packet(self):
        """Should build a valid JSON packet with all fields."""
        packet, command_id = build_command_packet("reboot", ["--force"], "node_001")
        data = json.loads(packet.decode("utf-8"))

        assert data["t"] == "cmd"
        assert data["cmd"] == "reboot"
        assert data["a"] == ["--force"]
        assert data["n"] == "node_001"
        assert "ts" in data
        assert "c" in data

    def test_returns_command_id(self):
        """Should return command_id in format timestamp_crcprefix."""
        packet, command_id = build_command_packet("test", [], "node_001")
        data = json.loads(packet.decode("utf-8"))

        # command_id should be {timestamp}_{first 4 chars of crc}
        expected_id = f"{data['ts']}_{data['c'][:4]}"
        assert command_id == expected_id

    def test_broadcast_has_empty_node_id(self):
        """Broadcast commands should have empty node_id."""
        packet, _ = build_command_packet("ping", [])
        data = json.loads(packet.decode("utf-8"))

        assert data["n"] == ""

    def test_packet_has_valid_crc(self):
        """Packet CRC should validate correctly."""
        packet, _ = build_command_packet("test", ["arg1", "arg2"], "node_001")
        data = json.loads(packet.decode("utf-8"))

        assert verify_crc(data, crc_key="c")

    def test_empty_args(self):
        """Empty args list should be preserved."""
        packet, _ = build_command_packet("ping", [])
        data = json.loads(packet.decode("utf-8"))

        assert data["a"] == []

    def test_multiple_args(self):
        """Multiple args should be preserved in order."""
        packet, _ = build_command_packet("set_config", ["key", "value", "option"])
        data = json.loads(packet.decode("utf-8"))

        assert data["a"] == ["key", "value", "option"]


class TestParseCommandPacket:
    """Tests for parse_command_packet function."""

    def test_roundtrip(self):
        """Build then parse should return equivalent data."""
        packet, _ = build_command_packet("reboot", ["--force"], "node_001")
        result = parse_command_packet(packet)

        assert result is not None
        assert isinstance(result, CommandPacket)
        assert result.command == "reboot"
        assert result.args == ["--force"]
        assert result.node_id == "node_001"
        assert isinstance(result.timestamp, int)

    def test_roundtrip_broadcast(self):
        """Broadcast command should parse correctly."""
        packet, _ = build_command_packet("ping", [])
        result = parse_command_packet(packet)

        assert result is not None
        assert result.command == "ping"
        assert result.node_id == ""
        assert result.is_broadcast() is True

    def test_is_broadcast_method(self):
        """is_broadcast should return correct value."""
        targeted, _ = build_command_packet("cmd", [], "node_001")
        broadcast, _ = build_command_packet("cmd", [])

        assert parse_command_packet(targeted).is_broadcast() is False
        assert parse_command_packet(broadcast).is_broadcast() is True

    def test_get_command_id(self):
        """Parsed packet should provide matching command_id."""
        packet, command_id = build_command_packet("test", [], "node_001")
        result = parse_command_packet(packet)

        assert result is not None
        assert result.get_command_id() == command_id

    def test_parse_invalid_json(self):
        """Invalid JSON should return None."""
        assert parse_command_packet(b"not json") is None

    def test_parse_invalid_crc(self):
        """Tampered packet should return None."""
        packet, _ = build_command_packet("test", [], "node_001")

        # Tamper with the packet
        data = json.loads(packet.decode("utf-8"))
        data["cmd"] = "tampered"
        tampered = json.dumps(data).encode("utf-8")

        assert parse_command_packet(tampered) is None

    def test_parse_wrong_type(self):
        """Packet with wrong type should return None."""
        # Build a sensor packet and try to parse as command
        readings = [make_reading("Test", "x", 1.0, "BME280TempPressureHumidity")]
        sensor_packet = build_lora_packets("test-node", readings)[0]

        assert parse_command_packet(sensor_packet) is None

    def test_parse_missing_fields(self):
        """Packet missing required fields should return None."""
        incomplete = json.dumps({"t": "cmd", "cmd": "test"}).encode("utf-8")
        assert parse_command_packet(incomplete) is None


# =============================================================================
# Command Registry Tests
# =============================================================================


class TestCommandRegistry:
    """Tests for CommandRegistry class."""

    def test_register_and_dispatch(self):
        """Registered handlers should be called on dispatch."""
        registry = CommandRegistry("node_001")
        called = []

        def handler(cmd: str, args: list[str]):
            called.append((cmd, args))

        registry.register("test_cmd", handler)
        handled, response = registry.dispatch("test_cmd", ["arg1"], "node_001")

        assert handled is True
        assert response is None
        assert called == [("test_cmd", ["arg1"])]

    def test_dispatch_unknown_command(self):
        """Dispatching unknown command should return False."""
        registry = CommandRegistry("node_001")
        handled, response = registry.dispatch("unknown", [], "node_001")

        assert handled is False
        assert response is None

    def test_scope_broadcast_only(self):
        """BROADCAST scope should only respond to broadcasts."""
        registry = CommandRegistry("node_001")
        called = []

        def handler(cmd: str, args: list[str]):
            called.append(cmd)

        registry.register("cmd", handler, CommandScope.BROADCAST)

        # Should be called for broadcast
        registry.dispatch("cmd", [], "")
        assert len(called) == 1

        # Should NOT be called for targeted
        registry.dispatch("cmd", [], "node_001")
        assert len(called) == 1

    def test_scope_private_only(self):
        """PRIVATE scope should only respond to targeted commands."""
        registry = CommandRegistry("node_001")
        called = []

        def handler(cmd: str, args: list[str]):
            called.append(cmd)

        registry.register("cmd", handler, CommandScope.PRIVATE)

        # Should NOT be called for broadcast
        registry.dispatch("cmd", [], "")
        assert len(called) == 0

        # Should be called for targeted
        registry.dispatch("cmd", [], "node_001")
        assert len(called) == 1

    def test_scope_any(self):
        """ANY scope should respond to both broadcast and targeted."""
        registry = CommandRegistry("node_001")
        called = []

        def handler(cmd: str, args: list[str]):
            called.append(cmd)

        registry.register("cmd", handler, CommandScope.ANY)

        # Should be called for broadcast
        registry.dispatch("cmd", [], "")
        assert len(called) == 1

        # Should be called for targeted
        registry.dispatch("cmd", [], "node_001")
        assert len(called) == 2

    def test_ignores_other_nodes(self):
        """Commands targeted to other nodes should be ignored."""
        registry = CommandRegistry("node_001")
        called = []

        def handler(cmd: str, args: list[str]):
            called.append(cmd)

        registry.register("cmd", handler, CommandScope.ANY)

        # Should be ignored - targeted to different node
        handled, response = registry.dispatch("cmd", [], "node_002")
        assert handled is False
        assert response is None
        assert len(called) == 0

    def test_multiple_handlers_same_command(self):
        """Multiple handlers for same command should all be called."""
        registry = CommandRegistry("node_001")
        called = []

        def handler1(cmd: str, args: list[str]):
            called.append("handler1")

        def handler2(cmd: str, args: list[str]):
            called.append("handler2")

        registry.register("cmd", handler1)
        registry.register("cmd", handler2)

        registry.dispatch("cmd", [], "node_001")
        assert called == ["handler1", "handler2"]

    def test_handler_exception_doesnt_stop_others(self):
        """Exception in one handler shouldn't prevent others from running."""
        registry = CommandRegistry("node_001")
        called = []

        def bad_handler(cmd: str, args: list[str]):
            raise RuntimeError("Simulated error")

        def good_handler(cmd: str, args: list[str]):
            called.append("good")

        registry.register("cmd", bad_handler)
        registry.register("cmd", good_handler)

        # Should still return True since good_handler ran
        handled, _ = registry.dispatch("cmd", [], "node_001")
        assert handled is True
        assert called == ["good"]

    def test_unregister(self):
        """Unregistered handlers should not be called."""
        registry = CommandRegistry("node_001")
        called = []

        def handler(cmd: str, args: list[str]):
            called.append(cmd)

        registry.register("cmd", handler)
        registry.unregister("cmd", handler)

        handled, response = registry.dispatch("cmd", [], "node_001")
        assert handled is False
        assert response is None
        assert len(called) == 0

    def test_dispatch_returns_handler_response(self):
        """Dispatch should return the first non-None response from handlers."""
        registry = CommandRegistry("node_001")

        def echo_handler(cmd: str, args: list[str]) -> dict | None:
            return {"data": args[0]} if args else None

        registry.register("echo", echo_handler, CommandScope.PRIVATE)
        handled, response = registry.dispatch("echo", ["hello"], "node_001")

        assert handled is True
        assert response == {"data": "hello"}

    def test_dispatch_returns_first_response(self):
        """When multiple handlers return responses, first non-None wins."""
        registry = CommandRegistry("node_001")

        def handler1(cmd: str, args: list[str]) -> dict | None:
            return {"from": "handler1"}

        def handler2(cmd: str, args: list[str]) -> dict | None:
            return {"from": "handler2"}

        registry.register("cmd", handler1)
        registry.register("cmd", handler2)
        handled, response = registry.dispatch("cmd", [], "node_001")

        assert handled is True
        assert response == {"from": "handler1"}

    def test_get_registered_commands(self):
        """Should return list of registered commands."""
        registry = CommandRegistry("node_001")

        registry.register("cmd1", lambda c, a: None)
        registry.register("cmd2", lambda c, a: None)
        registry.register("cmd1", lambda c, a: None)  # Duplicate

        commands = registry.get_registered_commands()
        assert sorted(commands) == ["cmd1", "cmd2"]


class TestEarlyAck:
    """Tests for earlyAck pattern in CommandRegistry."""

    def test_default_early_ack_is_true(self):
        """Default early_ack should be True."""
        registry = CommandRegistry("node_001")

        def handler(cmd: str, args: list[str]):
            pass

        registry.register("cmd", handler)
        entry = registry.lookup("cmd", "node_001")
        assert entry is not None
        assert entry.early_ack is True

    def test_early_ack_false(self):
        """early_ack=False should be preserved."""
        registry = CommandRegistry("node_001")

        def handler(cmd: str, args: list[str]) -> dict | None:
            return {"data": "response"}

        registry.register("echo", handler, CommandScope.PRIVATE, early_ack=False)
        entry = registry.lookup("echo", "node_001")
        assert entry is not None
        assert entry.early_ack is False

    def test_lookup_returns_matching_handler(self):
        """lookup should return the first matching handler."""
        registry = CommandRegistry("node_001")

        def handler(cmd: str, args: list[str]):
            pass

        registry.register("ping", handler, CommandScope.ANY, early_ack=True)
        entry = registry.lookup("ping", "node_001")
        assert entry is not None
        assert entry.early_ack is True

    def test_lookup_respects_scope(self):
        """lookup should respect scope filtering."""
        registry = CommandRegistry("node_001")

        def handler(cmd: str, args: list[str]):
            pass

        registry.register("cmd", handler, CommandScope.PRIVATE)

        # Should match for private
        assert registry.lookup("cmd", "node_001") is not None

        # Should NOT match for broadcast
        assert registry.lookup("cmd", "") is None

    def test_lookup_returns_none_for_unknown(self):
        """lookup should return None for unknown commands."""
        registry = CommandRegistry("node_001")
        assert registry.lookup("unknown", "node_001") is None

    def test_lookup_returns_none_for_other_nodes(self):
        """lookup should return None for commands targeted to other nodes."""
        registry = CommandRegistry("node_001")

        def handler(cmd: str, args: list[str]):
            pass

        registry.register("cmd", handler, CommandScope.ANY)
        assert registry.lookup("cmd", "node_002") is None

    def test_mixed_early_ack_handlers(self):
        """Registry should support both early and late ack handlers."""
        registry = CommandRegistry("node_001")

        def ping_handler(cmd: str, args: list[str]):
            pass

        def echo_handler(cmd: str, args: list[str]) -> dict | None:
            return {"data": args[0]} if args else None

        registry.register("ping", ping_handler, CommandScope.ANY, early_ack=True)
        registry.register("echo", echo_handler, CommandScope.PRIVATE, early_ack=False)

        ping_entry = registry.lookup("ping", "node_001")
        assert ping_entry is not None
        assert ping_entry.early_ack is True

        echo_entry = registry.lookup("echo", "node_001")
        assert echo_entry is not None
        assert echo_entry.early_ack is False

    def test_late_ack_dispatch_returns_response(self):
        """late-ack handler dispatch should return response payload."""
        registry = CommandRegistry("node_001")

        def echo_handler(cmd: str, args: list[str]) -> dict | None:
            return {"data": args[0]} if args else None

        registry.register("echo", echo_handler, CommandScope.PRIVATE, early_ack=False)

        # Verify it's a late-ack handler
        entry = registry.lookup("echo", "node_001")
        assert entry.early_ack is False

        # Dispatch should return the response
        handled, response = registry.dispatch("echo", ["hello"], "node_001")
        assert handled is True
        assert response == {"data": "hello"}


# =============================================================================
# ACK Packet Tests
# =============================================================================


class TestBuildAckPacket:
    """Tests for build_ack_packet function."""

    def test_builds_valid_packet(self):
        """Should build a valid JSON ACK packet with all fields."""
        packet = build_ack_packet("1699999999_a1b2", "node_001")
        data = json.loads(packet.decode("utf-8"))

        assert data["t"] == "ack"
        assert data["id"] == "1699999999_a1b2"
        assert data["n"] == "node_001"
        assert "c" in data

    def test_packet_has_valid_crc(self):
        """ACK packet CRC should validate correctly."""
        packet = build_ack_packet("1699999999_a1b2", "node_001")
        data = json.loads(packet.decode("utf-8"))

        assert verify_crc(data, crc_key="c")

    def test_different_command_ids_produce_different_packets(self):
        """Different command IDs should produce different packets."""
        packet1 = build_ack_packet("1699999999_a1b2", "node_001")
        packet2 = build_ack_packet("1699999998_c3d4", "node_001")

        assert packet1 != packet2

    def test_builds_packet_with_payload(self):
        """Should include payload in ACK packet when provided."""
        payload = {"data": "hello"}
        packet = build_ack_packet("1699999999_a1b2", "node_001", payload=payload)
        data = json.loads(packet.decode("utf-8"))

        assert data["p"] == {"data": "hello"}
        assert verify_crc(data, crc_key="c")

    def test_builds_packet_without_payload(self):
        """Should not include 'p' field when payload is None."""
        packet = build_ack_packet("1699999999_a1b2", "node_001")
        data = json.loads(packet.decode("utf-8"))

        assert "p" not in data


class TestParseAckPacket:
    """Tests for parse_ack_packet function."""

    def test_roundtrip(self):
        """Build then parse should return equivalent data."""
        command_id = "1699999999_a1b2"
        node_id = "node_001"
        packet = build_ack_packet(command_id, node_id)
        result = parse_ack_packet(packet)

        assert result is not None
        assert isinstance(result, AckPacket)
        assert result.command_id == command_id
        assert result.node_id == node_id
        assert result.payload is None

    def test_roundtrip_with_payload(self):
        """Build then parse should preserve payload."""
        command_id = "1699999999_a1b2"
        node_id = "node_001"
        payload = {"data": "echo_test_123"}
        packet = build_ack_packet(command_id, node_id, payload=payload)
        result = parse_ack_packet(packet)

        assert result is not None
        assert result.command_id == command_id
        assert result.node_id == node_id
        assert result.payload == {"data": "echo_test_123"}

    def test_parse_invalid_json(self):
        """Invalid JSON should return None."""
        assert parse_ack_packet(b"not json") is None

    def test_parse_invalid_crc(self):
        """Tampered ACK packet should return None."""
        packet = build_ack_packet("1699999999_a1b2", "node_001")

        # Tamper with the packet
        data = json.loads(packet.decode("utf-8"))
        data["id"] = "tampered"
        tampered = json.dumps(data).encode("utf-8")

        assert parse_ack_packet(tampered) is None

    def test_parse_wrong_type(self):
        """Packet with wrong type should return None."""
        # Build a command packet and try to parse as ACK
        cmd_packet, _ = build_command_packet("test", [], "node_001")
        assert parse_ack_packet(cmd_packet) is None

    def test_parse_sensor_packet_returns_none(self):
        """Sensor packet should return None."""
        readings = [make_reading("Test", "x", 1.0, "BME280TempPressureHumidity")]
        sensor_packet = build_lora_packets("test-node", readings)[0]

        assert parse_ack_packet(sensor_packet) is None

    def test_parse_missing_fields(self):
        """ACK packet missing required fields should return None."""
        incomplete = json.dumps({"t": "ack", "id": "test"}).encode("utf-8")
        assert parse_ack_packet(incomplete) is None


class TestCommandIdMatching:
    """Tests for command ID generation and matching."""

    def test_command_id_format(self):
        """Command ID should be timestamp_crcprefix format."""
        packet, command_id = build_command_packet("test", [], "node_001")
        data = json.loads(packet.decode("utf-8"))

        parts = command_id.split("_")
        assert len(parts) == 2
        assert parts[0] == str(data["ts"])
        assert parts[1] == data["c"][:4]

    def test_ack_matches_command(self):
        """ACK command_id should match the command's get_command_id()."""
        packet, command_id = build_command_packet("test", [], "node_001")
        cmd = parse_command_packet(packet)

        # Node receives command and sends ACK
        ack_packet = build_ack_packet(cmd.get_command_id(), "node_001")
        ack = parse_ack_packet(ack_packet)

        assert ack.command_id == command_id

    def test_different_commands_have_different_ids(self):
        """Different commands should have different IDs."""
        # Same command sent twice will have different timestamps
        _, id1 = build_command_packet("test", [], "node_001")

        # Force different timestamp by modifying time (in practice, different seconds)
        import time
        time.sleep(0.001)  # Ensure some time passes

        # Different command content produces different CRC
        _, id2 = build_command_packet("test2", [], "node_001")

        # At minimum the CRC prefix should differ
        assert id1 != id2


# =============================================================================
# CommandQueue Tests
# =============================================================================


class TestCommandQueue:
    """Tests for CommandQueue add/send/ack lifecycle."""

    def test_add_returns_command_id(self):
        """add() should return a command ID string."""
        q = CommandQueue(max_size=10)
        cid = q.add("ping", [], "node_001")
        assert cid is not None
        assert isinstance(cid, str)

    def test_add_rejects_when_full(self):
        """add() should return None when queue is full."""
        q = CommandQueue(max_size=2)
        assert q.add("cmd1", [], "n1") is not None
        assert q.add("cmd2", [], "n2") is not None
        assert q.add("cmd3", [], "n3") is None

    def test_get_next_to_send_promotes_from_queue(self):
        """get_next_to_send() should promote first queued command to current."""
        q = CommandQueue(max_size=10)
        cid = q.add("ping", [], "node_001")
        pending = q.get_next_to_send()
        assert pending is not None
        assert pending.command_id == cid

    def test_serial_queue_blocks_next_command(self):
        """Second command should not be sent while first is current."""
        q = CommandQueue(max_size=10)
        cid1 = q.add("cmd1", [], "n1")
        cid2 = q.add("cmd2", [], "n2")

        p1 = q.get_next_to_send()
        assert p1.command_id == cid1
        q.mark_sent()

        # Next get should still return cmd1 (waiting for ACK / retry)
        p_again = q.get_next_to_send()
        # It should be None since retry timer hasn't elapsed
        assert p_again is None or p_again.command_id == cid1

    def test_ack_retires_current(self):
        """ack_received() should retire the current command."""
        q = CommandQueue(max_size=10)
        cid = q.add("ping", [], "node_001")
        q.get_next_to_send()
        q.mark_sent()

        retired = q.ack_received(cid)
        assert retired is not None
        assert retired.command_id == cid
        assert not q.has_current()

    def test_ack_with_payload_stores_response(self):
        """ack_received() with payload should store it for wait_for_response."""
        q = CommandQueue(max_size=10)
        cid = q.add("echo", ["hello"], "node_001")
        q.get_next_to_send()
        q.mark_sent()

        q.ack_received(cid, payload={"data": "hello"})
        response = q.wait_for_response(cid, timeout=1.0)
        assert response == {"data": "hello"}


class TestCommandQueueCancel:
    """Tests for CommandQueue.cancel() method."""

    def test_cancel_current_command(self):
        """cancel() should remove the current command."""
        q = CommandQueue(max_size=10)
        cid = q.add("echo", ["hello"], "node_001")
        q.get_next_to_send()  # Promote to current
        q.mark_sent()

        assert q.has_current()
        assert q.cancel(cid) is True
        assert not q.has_current()

    def test_cancel_queued_command(self):
        """cancel() should remove a command waiting in the queue."""
        q = CommandQueue(max_size=10)
        cid1 = q.add("cmd1", [], "n1")
        cid2 = q.add("cmd2", [], "n2")

        q.get_next_to_send()  # cmd1 becomes current
        # cmd2 is still in the queue
        assert q.pending_count() == 1

        assert q.cancel(cid2) is True
        assert q.pending_count() == 0

    def test_cancel_nonexistent_returns_false(self):
        """cancel() should return False for unknown command ID."""
        q = CommandQueue(max_size=10)
        assert q.cancel("nonexistent_id") is False

    def test_cancel_unblocks_next_command(self):
        """After cancelling current, next command should become sendable."""
        q = CommandQueue(max_size=10)
        cid1 = q.add("cmd1", [], "n1")
        cid2 = q.add("cmd2", [], "n2")

        q.get_next_to_send()  # cmd1 becomes current
        q.mark_sent()

        # Cancel cmd1 - should unblock cmd2
        q.cancel(cid1)
        p2 = q.get_next_to_send()
        assert p2 is not None
        assert p2.command_id == cid2

    def test_cancel_empty_queue_returns_false(self):
        """cancel() on empty queue should return False."""
        q = CommandQueue(max_size=10)
        assert q.cancel("any_id") is False

    def test_cancel_already_acked_returns_false(self):
        """cancel() on a command already ACK'd should return False."""
        q = CommandQueue(max_size=10)
        cid = q.add("ping", [], "n1")
        q.get_next_to_send()
        q.mark_sent()

        q.ack_received(cid)
        assert q.cancel(cid) is False
