#!/usr/bin/env python3
"""
Outdoor sensor node broadcaster.

Reads configured sensors and broadcasts readings via LoRa at configurable
per-sensor intervals. Designed to run on a Pi Zero 2W with sensors and
LoRa radio attached.

Configuration is loaded from config/node_config.json:
{
    "node_id": "patio",
    "sensors": [
        {"class": "BME280TempPressureHumidity", "interval_sec": 60},
        {"class": "MMA8452Accelerometer", "interval_sec": 1}
    ],
    "broadcast_interval_sec": 30,
    "lora": {
        "frequency_mhz": 915.0,
        "tx_power": 23,
        "cs_pin": 24,
        "reset_pin": 25
    }
}

Each sensor can have its own "interval_sec". If not specified, falls back
to the global "broadcast_interval_sec" (default: 30s).

Usage:
    python3 node_broadcast.py [config_file]
"""

import argparse
import inspect
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import sensors as sensors_module
from radio import RFM9xRadio
from sensors import Sensor
from utils.protocol import SensorReading, build_lora_packets

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@dataclass
class SensorEntry:
    """A sensor instance with its broadcast configuration."""

    sensor: Sensor
    interval_sec: float
    last_broadcast: float = 0.0

    @property
    def class_name(self) -> str:
        """Get the sensor's class name."""
        return type(self.sensor).__name__


def load_config(config_path: str) -> dict:
    """Load node configuration from JSON file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        return json.load(f)


def get_sensor_class(class_name: str) -> type[Sensor] | None:
    """
    Get a Sensor class by name using reflection.

    Args:
        class_name: Name of the sensor class (e.g., "BME280TempPressureHumidity")

    Returns:
        The sensor class, or None if not found
    """
    for name, obj in inspect.getmembers(sensors_module, inspect.isclass):
        if name == class_name and issubclass(obj, Sensor) and obj is not Sensor:
            return obj
    return None


def instantiate_sensors(
    sensor_configs: list[dict], default_interval: float
) -> list[SensorEntry]:
    """
    Instantiate sensors from configuration.

    Args:
        sensor_configs: List of sensor config dicts with 'class', optional 'config',
                        and optional 'interval_sec'
        default_interval: Default interval for sensors without explicit interval_sec

    Returns:
        List of SensorEntry objects for successfully initialized sensors
    """
    sensors = []

    for config in sensor_configs:
        class_name = config.get("class")
        if not class_name:
            logger.warning("Sensor config missing 'class' field, skipping")
            continue

        sensor_class = get_sensor_class(class_name)
        if sensor_class is None:
            logger.warning(f"Unknown sensor class: {class_name}, skipping")
            continue

        try:
            # Get optional constructor arguments
            kwargs = config.get("config", {})
            sensor = sensor_class(**kwargs)
            sensor.init()

            # Get per-sensor interval or use default
            interval = config.get("interval_sec", default_interval)

            entry = SensorEntry(sensor=sensor, interval_sec=interval)
            sensors.append(entry)
            logger.info(f"Initialized sensor: {entry.class_name} (interval: {interval}s)")
        except Exception as e:
            logger.error(f"Failed to initialize {class_name}: {e}")

    return sensors


def read_sensors(entries: list[SensorEntry]) -> list[SensorReading]:
    """
    Read specified sensors and build a list of readings.

    Args:
        entries: List of SensorEntry objects to read

    Returns:
        List of SensorReading objects with current timestamps
    """
    readings = []
    timestamp = time.time()

    for entry in entries:
        try:
            values = entry.sensor.read()
            names = entry.sensor.get_names()
            units = entry.sensor.get_units()

            for value, name, unit in zip(values, names, units):
                readings.append(
                    SensorReading(
                        name=name,
                        units=unit,
                        value=value,
                        sensor_class=entry.class_name,
                        timestamp=timestamp,
                    )
                )
        except Exception as e:
            logger.error(f"Error reading {entry.class_name}: {e}")

    return readings


def broadcast_loop(
    radio: RFM9xRadio,
    node_id: str,
    sensors: list[SensorEntry],
) -> None:
    """
    Main broadcast loop with per-sensor intervals.

    Continuously reads sensors and broadcasts via LoRa based on each
    sensor's configured interval.

    Args:
        radio: Initialized radio instance
        node_id: This node's identifier
        sensors: List of SensorEntry objects with interval configuration
    """
    logger.info(f"Starting broadcast loop for node '{node_id}'")
    logger.info(f"Radio: {radio.frequency_mhz} MHz, TX power: {radio.tx_power} dBm")

    for entry in sensors:
        logger.info(f"  {entry.class_name}: every {entry.interval_sec}s")

    broadcast_count = 0

    while True:
        now = time.time()

        # Find sensors that are due for broadcast
        due_sensors = [
            entry
            for entry in sensors
            if (now - entry.last_broadcast) >= entry.interval_sec
        ]

        if due_sensors:
            try:
                # Read only sensors that are due
                readings = read_sensors(due_sensors)

                if readings:
                    # Build compact packets (auto-splits if too large)
                    packets = build_lora_packets(node_id, readings)

                    broadcast_count += 1
                    all_success = True
                    total_bytes = 0

                    for packet in packets:
                        success = radio.send(packet)
                        total_bytes += len(packet)
                        if not success:
                            all_success = False

                    # Update last broadcast time for sensors we just read
                    for entry in due_sensors:
                        entry.last_broadcast = now

                    sensor_names = ", ".join(e.class_name for e in due_sensors)
                    if all_success:
                        logger.info(
                            f"Broadcast #{broadcast_count}: {len(readings)} readings "
                            f"from [{sensor_names}], "
                            f"{len(packets)} packet(s), {total_bytes} bytes"
                        )
                    else:
                        logger.warning(
                            f"Broadcast #{broadcast_count} failed [{sensor_names}]"
                        )
                else:
                    logger.warning("No sensor readings available")

            except Exception as e:
                logger.error(f"Broadcast error: {e}")

        # Calculate sleep time until next sensor is due
        sleep_times = []
        for entry in sensors:
            time_since_last = now - entry.last_broadcast
            time_until_next = entry.interval_sec - time_since_last
            sleep_times.append(max(0.1, time_until_next))

        sleep_time = min(sleep_times) if sleep_times else 1.0
        time.sleep(sleep_time)


def main():
    parser = argparse.ArgumentParser(
        description="Outdoor sensor node broadcaster",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="config/node_config.json",
        help="Path to config file (default: config/node_config.json)",
    )
    args = parser.parse_args()

    # Load configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    node_id = config.get("node_id")
    if not node_id:
        logger.error("Config missing 'node_id'")
        sys.exit(1)

    # Get default broadcast interval
    default_interval = config.get("broadcast_interval_sec", 30)

    # Initialize sensors
    sensor_configs = config.get("sensors", [])
    if not sensor_configs:
        logger.error("Config has no sensors defined")
        sys.exit(1)

    sensors = instantiate_sensors(sensor_configs, default_interval)
    if not sensors:
        logger.error("No sensors could be initialized")
        sys.exit(1)

    # Initialize radio
    lora_config = config.get("lora", {})
    radio = RFM9xRadio(
        frequency_mhz=lora_config.get("frequency_mhz", 915.0),
        tx_power=lora_config.get("tx_power", 23),
        cs_pin=lora_config.get("cs_pin", 24),
        reset_pin=lora_config.get("reset_pin", 25),
    )

    try:
        radio.init()
        logger.info("Radio initialized")

        # Start broadcast loop
        broadcast_loop(radio, node_id, sensors)

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        radio.close()


if __name__ == "__main__":
    main()
