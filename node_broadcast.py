#!/usr/bin/env python3
"""
Outdoor sensor node broadcaster.

Reads configured sensors and broadcasts readings via LoRa at regular intervals.
Designed to run on a Pi Zero 2W with sensors and LoRa radio attached.

Configuration is loaded from config/node_config.json:
{
    "node_id": "patio",
    "sensors": [
        {"class": "BME280TempPressureHumidity"},
        {"class": "MMA8452Accelerometer"}
    ],
    "broadcast_interval_sec": 30,
    "lora": {
        "frequency_mhz": 915.0,
        "tx_power": 23,
        "cs_pin": 24,
        "reset_pin": 25
    }
}

Usage:
    python3 node_broadcast.py [config_file]
"""

import argparse
import inspect
import json
import logging
import sys
import time
from pathlib import Path

import sensors as sensors_module
from radio import RFM9xRadio
from sensors import Sensor
from utils.protocol import SensorReading, build_lora_message

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


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


def instantiate_sensors(sensor_configs: list[dict]) -> list[tuple[Sensor, str]]:
    """
    Instantiate sensors from configuration.

    Args:
        sensor_configs: List of sensor config dicts with 'class' and optional 'config'

    Returns:
        List of (sensor_instance, class_name) tuples for successfully initialized sensors
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
            sensors.append((sensor, class_name))
            logger.info(f"Initialized sensor: {class_name}")
        except Exception as e:
            logger.error(f"Failed to initialize {class_name}: {e}")

    return sensors


def read_all_sensors(sensors: list[tuple[Sensor, str]]) -> list[SensorReading]:
    """
    Read all sensors and build a list of readings.

    Args:
        sensors: List of (sensor_instance, class_name) tuples

    Returns:
        List of SensorReading objects with current timestamps
    """
    readings = []
    timestamp = time.time()

    for sensor, class_name in sensors:
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
            logger.error(f"Error reading {class_name}: {e}")

    return readings


def broadcast_loop(
    radio: RFM9xRadio,
    node_id: str,
    sensors: list[tuple[Sensor, str]],
    interval_sec: float,
) -> None:
    """
    Main broadcast loop.

    Continuously reads sensors and broadcasts via LoRa.

    Args:
        radio: Initialized radio instance
        node_id: This node's identifier
        sensors: List of (sensor_instance, class_name) tuples
        interval_sec: Seconds between broadcasts
    """
    logger.info(f"Starting broadcast loop for node '{node_id}'")
    logger.info(f"Broadcast interval: {interval_sec}s")
    logger.info(f"Radio: {radio.frequency_mhz} MHz, TX power: {radio.tx_power} dBm")

    broadcast_count = 0

    while True:
        try:
            # Read all sensors
            readings = read_all_sensors(sensors)

            if readings:
                # Build and send message
                message = build_lora_message(node_id, readings)
                success = radio.send(message)

                broadcast_count += 1
                if success:
                    logger.info(
                        f"Broadcast #{broadcast_count}: {len(readings)} readings, "
                        f"{len(message)} bytes"
                    )
                else:
                    logger.warning(f"Broadcast #{broadcast_count} failed")
            else:
                logger.warning("No sensor readings available")

        except Exception as e:
            logger.error(f"Broadcast error: {e}")

        time.sleep(interval_sec)


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

    # Initialize sensors
    sensor_configs = config.get("sensors", [])
    if not sensor_configs:
        logger.error("Config has no sensors defined")
        sys.exit(1)

    sensors = instantiate_sensors(sensor_configs)
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
        broadcast_interval = config.get("broadcast_interval_sec", 30)
        broadcast_loop(radio, node_id, sensors, broadcast_interval)

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        radio.close()


if __name__ == "__main__":
    main()
