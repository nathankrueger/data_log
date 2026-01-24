#!/usr/bin/env python3
"""Data logger that samples a callable at a configurable interval and writes to CSV."""

import argparse
import csv
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Union

from bme280 import init_bme280, c_to_f

class DataLogger:
    def __init__(
        self,
        data_source: Callable[[], Union[int, float]],
        csv_file: str,
        sample_period: float,
        flush_interval: int = 10,
    ):
        """
        Initialize the data logger.

        Args:
            data_source: Callable that returns a number (int or float)
            csv_file: Path to the CSV file to write
            sample_period: Time between samples in seconds
            flush_interval: Number of samples before flushing to disk
        """
        self.data_source = data_source
        self.csv_file = csv_file
        self.sample_period = sample_period
        self.flush_interval = flush_interval
        self._buffer: list[tuple[str, Union[int, float]]] = []
        self._sample_count = 0

    def _flush(self, file_handle, writer):
        """Flush buffered samples to disk."""
        for row in self._buffer:
            writer.writerow(row)
        file_handle.flush()
        self._buffer.clear()

    def run(self):
        """Start the data logging loop."""
        Path(self.csv_file).parent.mkdir(parents=True, exist_ok=True)
        with open(self.csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "value"])
            f.flush()

            try:
                while True:
                    timestamp = datetime.now().isoformat()
                    value = self.data_source()
                    self._buffer.append((timestamp, value))
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

    bme = init_bme280()
    def bme_data_source() -> float:
         return c_to_f(bme.get_temperature())

    logger = DataLogger(
        data_source=bme_data_source,
        csv_file=args.csv_file,
        sample_period=args.sample_period,
        flush_interval=args.flush_interval,
    )

    print(f"Logging to {args.csv_file} every {args.sample_period}s (Ctrl+C to stop)")
    logger.run()
