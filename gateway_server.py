#!/usr/bin/env python3
"""
Indoor gateway server.

Receives sensor data via LoRa from outdoor nodes, optionally reads local sensors,
and serves multiple Pi5 dashboard clients via TCP.

Configuration is loaded from config/gateway_config.json:
{
    "node_id": "indoor-gateway",
    "local_sensors": [
        {"class": "BME280TempPressureHumidity"}
    ],
    "local_sensor_interval_sec": 5,
    "tcp_port": 5000,
    "lora": {
        "enabled": true,
        "frequency_mhz": 915.0,
        "cs_pin": 24,
        "reset_pin": 25
    }
}

Usage:
    python3 gateway_server.py [config_file]
"""

import argparse
import asyncio
import inspect
import json
import logging
import sys
import threading
import time
from pathlib import Path

import sensors as sensors_module
from radio import RFM9xRadio
from sensors import Sensor
from utils.protocol import (
    MSG_TYPE_DATA,
    MSG_TYPE_DISCOVER,
    MSG_TYPE_SUBSCRIBE,
    DataReading,
    SensorInfo,
    SensorReading,
    build_data_message,
    build_error_message,
    build_sensors_response,
    make_sensor_id,
    parse_lora_message,
    parse_tcp_message,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# =============================================================================
# Sensor Cache
# =============================================================================


class SensorCache:
    """
    Thread-safe cache for sensor readings from LoRa and local sources.

    Stores readings indexed by sensor_id, along with metadata for discovery.
    """

    def __init__(self, gateway_id: str):
        self._gateway_id = gateway_id
        self._lock = threading.Lock()
        # sensor_id -> (SensorInfo, latest DataReading)
        self._sensors: dict[str, tuple[SensorInfo, DataReading | None]] = {}
        # Callbacks to notify when data updates
        self._update_callbacks: list[callable] = []

    def register_callback(self, callback: callable) -> None:
        """Register a callback to be called when data updates."""
        with self._lock:
            self._update_callbacks.append(callback)

    def unregister_callback(self, callback: callable) -> None:
        """Unregister an update callback."""
        with self._lock:
            if callback in self._update_callbacks:
                self._update_callbacks.remove(callback)

    def update_readings(
        self, node_id: str, readings: list[SensorReading], is_local: bool = False
    ) -> None:
        """
        Update cache with new sensor readings.

        Creates SensorInfo for new sensors, updates DataReadings for existing ones.
        """
        data_readings = []

        with self._lock:
            for reading in readings:
                sensor_id = make_sensor_id(node_id, reading.sensor_class, reading.name)

                # Create or update sensor info
                info = SensorInfo(
                    sensor_id=sensor_id,
                    node_id=node_id,
                    name=reading.name,
                    units=reading.units,
                    sensor_class=reading.sensor_class,
                    is_local=is_local,
                )

                # Create data reading
                data_reading = DataReading(
                    sensor_id=sensor_id,
                    value=reading.value,
                    timestamp=reading.timestamp,
                )

                self._sensors[sensor_id] = (info, data_reading)
                data_readings.append(data_reading)

            # Copy callbacks to call outside lock
            callbacks = list(self._update_callbacks)

        # Notify subscribers (outside lock to avoid deadlocks)
        for callback in callbacks:
            try:
                callback(data_readings)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def get_all_sensors(self) -> list[SensorInfo]:
        """Get metadata for all known sensors."""
        with self._lock:
            return [info for info, _ in self._sensors.values()]

    def get_all_readings(self) -> list[DataReading]:
        """Get latest readings for all sensors."""
        with self._lock:
            return [reading for _, reading in self._sensors.values() if reading]

    @property
    def gateway_id(self) -> str:
        return self._gateway_id


# =============================================================================
# LoRa Receiver Thread
# =============================================================================


class LoRaReceiver(threading.Thread):
    """Background thread that receives LoRa packets and updates the cache."""

    def __init__(self, radio: RFM9xRadio, cache: SensorCache):
        super().__init__(daemon=True)
        self._radio = radio
        self._cache = cache
        self._running = False

    def run(self) -> None:
        self._running = True
        logger.info("LoRa receiver started")

        while self._running:
            try:
                packet = self._radio.receive(timeout=5.0)
                if packet is not None:
                    self._process_packet(packet)
            except Exception as e:
                logger.error(f"LoRa receive error: {e}")
                time.sleep(1)  # Back off on error

    def stop(self) -> None:
        self._running = False

    def _process_packet(self, packet: bytes) -> None:
        """Validate CRC, parse JSON, update sensor cache."""
        rssi = self._radio.get_last_rssi()

        result = parse_lora_message(packet)
        if result is None:
            logger.warning(f"Invalid LoRa packet (RSSI: {rssi} dB): {packet[:50]}...")
            return

        node_id, readings = result
        logger.info(
            f"LoRa received from '{node_id}': {len(readings)} readings (RSSI: {rssi} dB)"
        )

        self._cache.update_readings(node_id, readings, is_local=False)


# =============================================================================
# Local Sensor Reader Thread
# =============================================================================


class LocalSensorReader(threading.Thread):
    """Background thread that reads local sensors and updates the cache."""

    def __init__(
        self,
        node_id: str,
        sensors: list[tuple[Sensor, str]],
        cache: SensorCache,
        interval_sec: float = 5.0,
    ):
        super().__init__(daemon=True)
        self._node_id = node_id
        self._sensors = sensors
        self._cache = cache
        self._interval_sec = interval_sec
        self._running = False

    def run(self) -> None:
        self._running = True
        logger.info(
            f"Local sensor reader started ({len(self._sensors)} sensors, "
            f"{self._interval_sec}s interval)"
        )

        while self._running:
            try:
                readings = self._read_sensors()
                if readings:
                    self._cache.update_readings(
                        self._node_id, readings, is_local=True
                    )
            except Exception as e:
                logger.error(f"Local sensor read error: {e}")

            time.sleep(self._interval_sec)

    def stop(self) -> None:
        self._running = False

    def _read_sensors(self) -> list[SensorReading]:
        """Read all local sensors and return readings."""
        readings = []
        timestamp = time.time()

        for sensor, class_name in self._sensors:
            try:
                values = sensor.read()
                names = sensor.get_names()
                units = sensor.get_units()

                for value, name, unit in zip(values, names, units):
                    readings.append(
                        SensorReading(
                            name=name,
                            units=unit,
                            value=value,
                            sensor_class=class_name,
                            timestamp=timestamp,
                        )
                    )
            except Exception as e:
                logger.error(f"Error reading local {class_name}: {e}")

        return readings


# =============================================================================
# TCP Server
# =============================================================================


class GatewayTCPServer:
    """Multi-client TCP server handling discovery and data streaming."""

    def __init__(self, cache: SensorCache, host: str = "0.0.0.0", port: int = 5000):
        self._cache = cache
        self._host = host
        self._port = port
        self._server = None

    async def start(self) -> None:
        """Start the TCP server."""
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port
        )
        addr = self._server.sockets[0].getsockname()
        logger.info(f"TCP server listening on {addr[0]}:{addr[1]}")

        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop the TCP server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single client connection."""
        addr = writer.get_extra_info("peername")
        logger.info(f"Client connected: {addr}")

        try:
            while True:
                data = await reader.readline()
                if not data:
                    break  # Client disconnected

                await self._process_message(data, reader, writer)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Client {addr} error: {e}")
        finally:
            logger.info(f"Client disconnected: {addr}")
            writer.close()
            await writer.wait_closed()

    async def _process_message(
        self,
        data: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Process a single message from a client."""
        message = parse_tcp_message(data)
        if message is None:
            writer.write(build_error_message("Invalid message format"))
            await writer.drain()
            return

        msg_type = message.get("type")

        if msg_type == MSG_TYPE_DISCOVER:
            await self._handle_discover(writer)
        elif msg_type == MSG_TYPE_SUBSCRIBE:
            await self._handle_subscribe(reader, writer)
        else:
            writer.write(build_error_message(f"Unknown message type: {msg_type}"))
            await writer.drain()

    async def _handle_discover(self, writer: asyncio.StreamWriter) -> None:
        """Handle a discover request."""
        sensors = self._cache.get_all_sensors()
        response = build_sensors_response(self._cache.gateway_id, sensors)
        writer.write(response)
        await writer.drain()
        logger.info(f"Sent discovery response: {len(sensors)} sensors")

    async def _handle_subscribe(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a subscribe request - stream data updates to client."""
        addr = writer.get_extra_info("peername")
        logger.info(f"Client {addr} subscribed to data stream")

        # Create async queue for this client
        queue: asyncio.Queue[list[DataReading]] = asyncio.Queue()

        # Callback to push data to queue (called from other threads)
        def on_data(readings: list[DataReading]) -> None:
            try:
                # Use call_soon_threadsafe since callback is from another thread
                asyncio.get_event_loop().call_soon_threadsafe(
                    queue.put_nowait, readings
                )
            except Exception:
                pass  # Queue might be closed

        self._cache.register_callback(on_data)

        try:
            # Send initial data
            initial_readings = self._cache.get_all_readings()
            if initial_readings:
                writer.write(build_data_message(initial_readings))
                await writer.drain()

            # Stream updates
            while True:
                # Wait for new data with timeout (allows checking if client disconnected)
                try:
                    readings = await asyncio.wait_for(queue.get(), timeout=30.0)
                    writer.write(build_data_message(readings))
                    await writer.drain()
                except asyncio.TimeoutError:
                    # Send heartbeat/keepalive (empty data message)
                    writer.write(build_data_message([]))
                    await writer.drain()

        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._cache.unregister_callback(on_data)
            logger.info(f"Client {addr} unsubscribed from data stream")


# =============================================================================
# Sensor Instantiation
# =============================================================================


def get_sensor_class(class_name: str) -> type[Sensor] | None:
    """Get a Sensor class by name using reflection."""
    for name, obj in inspect.getmembers(sensors_module, inspect.isclass):
        if name == class_name and issubclass(obj, Sensor) and obj is not Sensor:
            return obj
    return None


def instantiate_sensors(sensor_configs: list[dict]) -> list[tuple[Sensor, str]]:
    """Instantiate sensors from configuration."""
    sensors = []

    for config in sensor_configs:
        class_name = config.get("class")
        if not class_name:
            continue

        sensor_class = get_sensor_class(class_name)
        if sensor_class is None:
            logger.warning(f"Unknown sensor class: {class_name}")
            continue

        try:
            kwargs = config.get("config", {})
            sensor = sensor_class(**kwargs)
            sensor.init()
            sensors.append((sensor, class_name))
            logger.info(f"Initialized local sensor: {class_name}")
        except Exception as e:
            logger.error(f"Failed to initialize {class_name}: {e}")

    return sensors


# =============================================================================
# Main
# =============================================================================


def load_config(config_path: str) -> dict:
    """Load gateway configuration from JSON file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        return json.load(f)


async def run_gateway(config: dict) -> None:
    """Run the gateway server."""
    node_id = config.get("node_id", "gateway")

    # Create sensor cache
    cache = SensorCache(node_id)

    # Start LoRa receiver if enabled
    lora_receiver = None
    radio = None
    lora_config = config.get("lora", {})

    if lora_config.get("enabled", True):
        try:
            radio = RFM9xRadio(
                frequency_mhz=lora_config.get("frequency_mhz", 915.0),
                tx_power=lora_config.get("tx_power", 23),
                cs_pin=lora_config.get("cs_pin", 24),
                reset_pin=lora_config.get("reset_pin", 25),
            )
            radio.init()
            lora_receiver = LoRaReceiver(radio, cache)
            lora_receiver.start()
            logger.info(f"LoRa receiver enabled at {radio.frequency_mhz} MHz")
        except Exception as e:
            logger.error(f"Failed to initialize LoRa: {e}")
            logger.info("Continuing without LoRa receiver")

    # Start local sensor reader if configured
    local_reader = None
    local_sensor_configs = config.get("local_sensors", [])

    if local_sensor_configs:
        local_sensors = instantiate_sensors(local_sensor_configs)
        if local_sensors:
            interval = config.get("local_sensor_interval_sec", 5.0)
            local_reader = LocalSensorReader(node_id, local_sensors, cache, interval)
            local_reader.start()

    # Start TCP server
    tcp_port = config.get("tcp_port", 5000)
    tcp_server = GatewayTCPServer(cache, port=tcp_port)

    try:
        await tcp_server.start()
    finally:
        # Cleanup
        if lora_receiver:
            lora_receiver.stop()
        if local_reader:
            local_reader.stop()
        if radio:
            radio.close()


def main():
    parser = argparse.ArgumentParser(
        description="Indoor gateway server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="config/gateway_config.json",
        help="Path to config file (default: config/gateway_config.json)",
    )
    args = parser.parse_args()

    # Load configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    # Run gateway
    try:
        asyncio.run(run_gateway(config))
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main()
