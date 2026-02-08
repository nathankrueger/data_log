#!/usr/bin/env python3
"""
Stream GPS data from GT-U7 (NEO-6M compatible) GPS module.

The GT-U7 uses a UBLOX 7th generation chip and outputs NMEA sentences
over UART. This script parses GGA sentences to extract position data.

Wiring (GT-U7 to Pi):
    VCC -> 5V (or 3.3V)
    GND -> GND
    TX  -> GPIO 15 (UART RX)
    RX  -> GPIO 14 (UART TX) - optional, only needed for configuration

Enable UART on Pi:
    sudo raspi-config -> Interface Options -> Serial Port
    - Login shell over serial: No
    - Serial port hardware enabled: Yes
    Reboot after changing settings.

Usage:
    python3 gps_stream.py                    # Default /dev/serial0 at 9600 baud
    python3 gps_stream.py -p /dev/ttyUSB0    # USB GPS adapter
    python3 gps_stream.py -b 115200          # Different baud rate

Requires:
    pip3 install pyserial pynmea2
"""

import argparse
import sys
import time

import serial
import pynmea2


def stream_gps(port: str, baud: int) -> None:
    """Stream GPS data continuously."""
    print(f"Opening {port} at {baud} baud...")
    print("Waiting for GPS fix... (this may take 30-60 seconds outdoors)")
    print("-" * 60)
    print(f"{'Latitude':>12}  {'Longitude':>12}  {'Alt (m)':>8}  {'Sats':>4}")
    print("-" * 60)

    with serial.Serial(port, baud, timeout=1) as ser:
        while True:
            try:
                line = ser.readline().decode("ascii", errors="replace").strip()
                if not line:
                    continue

                # Parse NMEA sentence
                if line.startswith("$"):
                    try:
                        msg = pynmea2.parse(line)

                        # GGA contains lat, lon, altitude, and satellite count
                        if isinstance(msg, pynmea2.GGA):
                            lat = msg.latitude if msg.latitude else None
                            lon = msg.longitude if msg.longitude else None
                            alt = msg.altitude if msg.altitude else None
                            sats = msg.num_sats if msg.num_sats else "0"

                            if lat is not None and lon is not None:
                                alt_str = f"{alt:.1f}" if alt is not None else "N/A"
                                print(f"{lat:>12.6f}  {lon:>12.6f}  {alt_str:>8}  {sats:>4}")
                            else:
                                print(f"{'No fix':>12}  {'':>12}  {'':>8}  {sats:>4}", end="\r")

                    except pynmea2.ParseError:
                        # Corrupted sentence, skip it
                        pass

            except KeyboardInterrupt:
                print("\nStopped.")
                break
            except serial.SerialException as e:
                print(f"Serial error: {e}")
                time.sleep(1)


def main():
    parser = argparse.ArgumentParser(
        description="Stream GPS data from GT-U7/NEO-6M GPS module."
    )
    parser.add_argument(
        "-p", "--port",
        default="/dev/serial0",
        help="Serial port (default: /dev/serial0)"
    )
    parser.add_argument(
        "-b", "--baud",
        type=int,
        default=9600,
        help="Baud rate (default: 9600)"
    )
    args = parser.parse_args()

    try:
        stream_gps(args.port, args.baud)
    except PermissionError:
        print(f"Permission denied for {args.port}")
        print("Try: sudo usermod -a -G dialout $USER && logout")
        sys.exit(1)
    except FileNotFoundError:
        print(f"Serial port {args.port} not found")
        print("Check wiring and that UART is enabled (raspi-config)")
        sys.exit(1)


if __name__ == "__main__":
    main()
