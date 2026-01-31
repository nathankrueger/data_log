#!/usr/bin/env python3
"""
LoRa radio communication test for RFM9x and SX1262 modules on Raspberry Pi.

Supports testing interoperability between different radio types by selecting
the radio module via command line.

Usage:
    # Test with RFM9x (default)
    python3 scripts/radio_test.py -s            # Send with RFM9x
    python3 scripts/radio_test.py -r            # Receive with RFM9x

    # Test with SX1262
    python3 scripts/radio_test.py -s -t sx1262  # Send with SX1262
    python3 scripts/radio_test.py -r -t sx1262  # Receive with SX1262

    # Cross-radio interoperability test (run on two different Pis)
    python3 scripts/radio_test.py -s -t rfm9x   # Pi 1: Send with RFM9x
    python3 scripts/radio_test.py -r -t sx1262  # Pi 2: Receive with SX1262

Requires:
    RFM9x:  pip install adafruit-circuitpython-rfm9x
    SX1262: pip install sx1262
"""

import argparse
import sys
import time
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radio import Radio, RFM9xRadio, SX1262Radio, rssi_to_brightness
from sensors import BME280TempPressureHumidity
from utils.led import RgbLed


def create_radio(args) -> Radio:
    """Create the appropriate radio instance based on args."""
    if args.type == "rfm9x":
        return RFM9xRadio(
            frequency_mhz=args.frequency,
            tx_power=args.tx_power,
            cs_pin=args.cs_pin,
            reset_pin=args.reset_pin,
        )
    elif args.type == "sx1262":
        return SX1262Radio(
            frequency_mhz=args.frequency,
            tx_power=min(args.tx_power, 22),  # SX1262 max is 22 dBm
            busy_pin=args.busy_pin,
            reset_pin=args.reset_pin,
            dio1_pin=args.dio1_pin,
            txen_pin=args.txen_pin,
            rxen_pin=args.rxen_pin,
            spi_bus=args.spi_bus,
            spi_cs=args.spi_cs,
        )
    else:
        raise ValueError(f"Unknown radio type: {args.type}")


def send_messages(radio: Radio, use_sensor: bool = True) -> None:
    """Continuously send messages, optionally with BME280 sensor data."""
    counter = 0
    bme = None

    if use_sensor:
        try:
            bme = BME280TempPressureHumidity()
            bme.init()
            print("BME280 sensor initialized")
        except Exception as e:
            print(f"BME280 not available ({e}), sending counter only")
            bme = None

    print(f"Radio: {type(radio).__name__}")
    print(f"Frequency: {radio.frequency_mhz} MHz, TX power: {radio.tx_power} dBm")
    print("Sending messages... (Ctrl+C to stop)\n")

    while True:
        if bme:
            try:
                temp, pressure, humidity = bme.read()
                message = f"T:{temp:.1f}F P:{pressure:.1f}hPa H:{humidity:.1f}% #{counter}"
            except Exception:
                message = f"Counter: {counter}"
        else:
            message = f"Counter: {counter}"

        print(f"Sending: {message}")
        success = radio.send(message.encode("utf-8"))
        if not success:
            print("  -> Send failed!")
        counter += 1
        time.sleep(2)


def receive_messages(radio: Radio, led: RgbLed | None = None) -> None:
    """Listen for incoming messages."""
    print(f"Radio: {type(radio).__name__}")
    print(f"Frequency: {radio.frequency_mhz} MHz")
    print("Waiting for messages... (Ctrl+C to stop)\n")

    while True:
        packet = radio.receive(timeout=5.0)
        if packet is not None:
            rssi = radio.get_last_rssi()

            if led:
                brightness = rssi_to_brightness(rssi) if rssi else 0
                led.flash(brightness, 0, 0, 0.5)

            try:
                message = packet.decode("utf-8")
                print(f"Received: {message} (RSSI: {rssi} dBm)")
            except UnicodeDecodeError:
                print(f"Received raw bytes: {packet!r} (RSSI: {rssi} dBm)")
        else:
            print("No message received, still listening...")


def main():
    parser = argparse.ArgumentParser(
        description="LoRa radio test for RFM9x and SX1262 interoperability",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Mode selection
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("-s", "--send", action="store_true", help="Send messages")
    mode_group.add_argument("-r", "--receive", action="store_true", help="Receive messages")

    # Radio type selection
    parser.add_argument(
        "-t", "--type",
        choices=["rfm9x", "sx1262"],
        default="rfm9x",
        help="Radio module type (default: rfm9x)",
    )

    # Common radio options
    parser.add_argument(
        "-f", "--frequency",
        type=float,
        default=915.0,
        help="Frequency in MHz (default: 915.0)",
    )
    parser.add_argument(
        "-p", "--tx-power",
        type=int,
        default=23,
        help="TX power in dBm (default: 23, max 22 for SX1262)",
    )
    parser.add_argument(
        "--reset-pin",
        type=int,
        default=None,
        help="Reset GPIO pin (default: 25 for RFM9x, 22 for SX1262)",
    )

    # RFM9x-specific options
    rfm_group = parser.add_argument_group("RFM9x options")
    rfm_group.add_argument("--cs-pin", type=int, default=24, help="CS GPIO pin (default: 24)")

    # SX1262-specific options
    sx_group = parser.add_argument_group("SX1262 options")
    sx_group.add_argument("--busy-pin", type=int, default=18, help="BUSY GPIO pin (default: 18)")
    sx_group.add_argument("--dio1-pin", type=int, default=16, help="DIO1 GPIO pin (default: 16)")
    sx_group.add_argument("--txen-pin", type=int, default=6, help="TXEN GPIO pin (default: 6)")
    sx_group.add_argument("--rxen-pin", type=int, default=5, help="RXEN GPIO pin (default: 5)")
    sx_group.add_argument("--spi-bus", type=int, default=0, help="SPI bus (default: 0)")
    sx_group.add_argument("--spi-cs", type=int, default=0, help="SPI chip select (default: 0)")

    # Other options
    parser.add_argument("--no-led", action="store_true", help="Disable LED feedback on receive")
    parser.add_argument("--no-sensor", action="store_true", help="Don't read BME280 sensor when sending")

    args = parser.parse_args()

    # Set default reset pin based on radio type if not specified
    if args.reset_pin is None:
        args.reset_pin = 25 if args.type == "rfm9x" else 22

    led = None
    radio = create_radio(args)

    try:
        radio.init()
        print(f"Radio initialized successfully\n")

        if args.send:
            send_messages(radio, use_sensor=not args.no_sensor)
        else:
            if not args.no_led:
                try:
                    led = RgbLed(red_bcm=17, green_bcm=27, blue_bcm=22, common_anode=True)
                except Exception as e:
                    print(f"LED not available ({e}), continuing without LED feedback")
            receive_messages(radio, led)

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        radio.close()
        if led:
            led.close()


if __name__ == "__main__":
    main()
