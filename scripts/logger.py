#!/usr/bin/env python3
"""Data logger that samples a callable at a configurable interval and writes to CSV."""

import argparse
import atexit
import csv
import inspect
import json
import signal
import time
from datetime import datetime
from pathlib import Path

import sensors as sensors_module
from sensors import Sensor
from utils import RgbLed

SAMPLE_FLASH_S = 0.2
SAVING_FILE_S = 0.5


class DataLogger:
    def __init__(
        self,
        sensors: list[Sensor],
        csv_file: str,
        sample_period: float,
        flush_interval: int = 10,
        led: RgbLed | None = None,
        rec_sample_color: tuple[int, int, int] = (0, 0, 255),
        saving_file_color: tuple[int, int, int] = (15, 0, 255),
    ):
        """
        Initialize the data logger.

        Args:
            sensors: List of Sensor objects to read from
            csv_file: Path to the CSV file to write
            sample_period: Time between samples in seconds
            flush_interval: Number of samples before flushing to disk
            led: Optional RgbLed instance for visual feedback
            rec_sample_color: RGB color to flash when recording a sample
            saving_file_color: RGB color to flash when flushing to disk
        """
        self.sensors = sensors
        self.csv_file = csv_file
        self.sample_period = sample_period
        self.flush_interval = flush_interval
        self.led = led
        self.rec_sample_color = rec_sample_color
        self.saving_file_color = saving_file_color
        self._buffer: list[list] = []
        self._sample_count = 0

    def init_sensors(self) -> None:
        """Initialize all sensors."""
        for sensor in self.sensors:
            sensor.init()

    def _flush(self, file_handle, writer):
        """Flush buffered samples to disk."""
        if self.led:
            self.led.flash(*self.saving_file_color, SAVING_FILE_S)
        for row in self._buffer:
            writer.writerow(row)
        file_handle.flush()
        self._buffer.clear()

    def run(self):
        """Start the data logging loop."""
        Path(self.csv_file).parent.mkdir(parents=True, exist_ok=True)
        with open(self.csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            header = ["timestamp"]
            for s in self.sensors:
                names = s.get_names()
                units = s.get_units()
                for name, unit in zip(names, units):
                    header.append(f"{name} ({unit})")
            writer.writerow(header)
            f.flush()

            try:
                while True:
                    if self.led:
                        self.led.flash(*self.rec_sample_color, SAMPLE_FLASH_S)

                    timestamp = datetime.now().isoformat()
                    row = [timestamp]
                    for s in self.sensors:
                        row.extend(s.read())
                    self._buffer.append(row)
                    self._sample_count += 1

                    if self._sample_count % self.flush_interval == 0:
                        self._flush(f, writer)

                    time.sleep(self.sample_period)
            except KeyboardInterrupt:
                if self._buffer:
                    self._flush(f, writer)
                print(f"\nLogged {self._sample_count} samples to {self.csv_file}")

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


def instantiate_sensors(sensor_configs: list[dict]) -> list[Sensor]:
    """
    Instantiate sensors from configuration.

    Args:
        sensor_configs: List of sensor config dicts with 'class' and optional 'config'

    Returns:
        List of sensor instances for successfully found sensors
    """
    sensors = []

    for config in sensor_configs:
        class_name = config.get("class")
        if not class_name:
            print(f"Warning: Sensor config missing 'class' field, skipping")
            continue

        sensor_class = get_sensor_class(class_name)
        if sensor_class is None:
            print(f"Warning: Unknown sensor class: {class_name}, skipping")
            continue

        kwargs = config.get("config", {})
        sensor = sensor_class(**kwargs)
        sensors.append(sensor)
        print(f"Loaded sensor: {class_name}")

    return sensors


if __name__ == "__main__":
    script_dir = Path(__file__).parent.resolve()
    default_config = script_dir / "logger.json"

    parser = argparse.ArgumentParser(description="Log data from a callable to CSV")
    parser.add_argument(
        "--csv_file",
        type=str,
        required=True,
        help="Path to the output CSV file",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(default_config),
        help=f"Path to the JSON config file (default: {default_config})",
    )
    parser.add_argument(
        "--sample_period",
        type=float,
        default=1.0,
        help="Time between samples in seconds",
    )
    parser.add_argument(
        "--flush_interval",
        type=int,
        default=10,
        help="Number of samples before flushing to disk (default: 10)",
    )
    args = parser.parse_args()

    # Load config
    with open(args.config) as f:
        config = json.load(f)

    running_color = tuple(config.get("running_color", [0, 255, 0]))
    rec_sample_color = tuple(config.get("rec_sample_color", [0, 0, 255]))
    saving_file_color = tuple(config.get("saving_file_color", [15, 0, 255]))

    # Initialize LED
    led = RgbLed(
        red_bcm=config["led_r_bcm"],
        green_bcm=config["led_g_bcm"],
        blue_bcm=config["led_b_bcm"],
        common_anode=config["led_common_anode"],
    )

    # Set running color
    led.set_base_color(*running_color)

    # Register cleanup to turn off LED on exit
    def cleanup():
        led.off()
        led.close()

    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, lambda sig, frame: (cleanup(), exit(0)))

    # Initialize sensors from config
    sensor_configs = config.get("sensors", [])
    if not sensor_configs:
        print("Error: Config has no sensors defined")
        exit(1)

    sensors = instantiate_sensors(sensor_configs)
    if not sensors:
        print("Error: No sensors could be loaded")
        exit(1)

    logger = DataLogger(
        sensors=sensors,
        csv_file=args.csv_file,
        sample_period=args.sample_period,
        flush_interval=args.flush_interval,
        led=led,
        rec_sample_color=rec_sample_color,
        saving_file_color=saving_file_color,
    )

    logger.init_sensors()
    print(f"Logging to {args.csv_file} every {args.sample_period}s (Ctrl+C to stop)")
    logger.run()
