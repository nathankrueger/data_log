#!/usr/bin/env python3
"""Data logger that samples a callable at a configurable interval and writes to CSV."""

import argparse
import atexit
import csv
import json
import signal
import time
from datetime import datetime
from pathlib import Path

from led import RgbLed
from sensors import BME280TempPressureHumidity, MMA8452Accelerometer, Sensor

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

if __name__ == "__main__":
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
        default="logger.json",
        help="Path to the JSON config file (default: logger.json)",
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

    logger = DataLogger(
        sensors=[BME280TempPressureHumidity(), MMA8452Accelerometer()],
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
