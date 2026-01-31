#!/usr/bin/env python3
"""
Simple LoRa radio communication demo using RFM9x module on Raspberry Pi.

This is a CLI demo for testing the radio. For production use, see:
- node_broadcast.py (outdoor sensor node)
- gateway_server.py (indoor gateway)

Wiring (RFM9x to Pi):
    VIN  -> 3.3V
    GND  -> GND
    SCK  -> GPIO 11 (SPI0 SCLK)
    MISO -> GPIO 9  (SPI0 MISO)
    MOSI -> GPIO 10 (SPI0 MOSI)
    CS   -> GPIO 24 (configurable)
    RST  -> GPIO 25 (configurable)

Usage:
    python3 radio_test.py -s    # Run on transmitter Pi
    python3 radio_test.py -r    # Run on receiver Pi

Requires:
    pip3 install adafruit-circuitpython-rfm9x
"""

import argparse
import sys
import time
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.led import RgbLed
from radio import RFM9xRadio, rssi_to_brightness
from sensors import BME280TempPressureHumidity


def send_messages(radio: RFM9xRadio, show_temp: bool) -> None:
    """Continuously send messages with BME280 temperature, pressure, and humidity."""
    counter = 0

    bme = None
    if show_temp:
        bme = BME280TempPressureHumidity()
        bme.init()

    print(f"Radio initialized at {radio.frequency_mhz} MHz, TX power: {radio.tx_power} dBm")
    print("Sending messages... (Ctrl+C to stop)")

    while True:
        if show_temp:
            temp, pressure, humidity = bme.read()
            message = f"T:{temp:.1f}F P:{pressure:.1f}hPa H:{humidity:.1f}% #{counter}"
        else:
            message = f"Hello World #{counter}"
        print(f"Sending: {message}")
        radio.send(message.encode("utf-8"))
        counter += 1
        time.sleep(2)


def receive_messages(radio: RFM9xRadio, led: RgbLed) -> None:
    """Listen for incoming messages."""
    print(f"Radio initialized at {radio.frequency_mhz} MHz")
    print("Waiting for messages... (Ctrl+C to stop)")

    while True:
        packet = radio.receive(timeout=5.0)
        if packet is not None:
            rssi = radio.get_last_rssi()

            if led:
                brightness = rssi_to_brightness(rssi) if rssi else 0
                led.flash(brightness, 0, 0, 0.5)  # Flash red, brightness based on RSSI
            try:
                message = packet.decode("utf-8")
                print(f"Received: {message} (RSSI: {rssi} dB, brightness: {brightness})")
            except UnicodeDecodeError:
                print(f"Received raw bytes: {packet} (RSSI: {rssi} dB)")
        else:
            print("No message received, still listening...")


def main():
    parser = argparse.ArgumentParser(
        description="Simple LoRa radio communication demo using RFM9x module."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-s", "--send", action="store_true", help="Send messages")
    group.add_argument("-r", "--receive", action="store_true", help="Receive messages")
    group.add_argument("-t", "--temp", action="store_true", help="Send Temperature Values")
    group.add_argument("--no_led", action="store_true", help="Skip using LED to show RSSI")
    args = parser.parse_args()

    led = None

    # Create radio with default configuration
    radio = RFM9xRadio(
        frequency_mhz=915.0,
        tx_power=23,
        cs_pin=24,
        reset_pin=25,
    )

    try:
        radio.init()

        if args.send:
            send_messages(radio, args.temp)
        else:
            led = None
            if not args.no_led:
                led = RgbLed(red_bcm=17, green_bcm=27, blue_bcm=22, common_anode=True)
            receive_messages(radio, led)
    finally:
        radio.close()
        if led:
            led.close()


if __name__ == "__main__":
    main()
