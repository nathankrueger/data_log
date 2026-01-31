#!/usr/bin/env python3
"""
SSD1306 OLED Display with BME280 Sensor Demo (128x64 pixels, I2C)

Wiring (both devices on same I2C bus):
  VCC -> 3.3V
  GND -> GND
  SDA -> GPIO2 (Pin 3)
  SCL -> GPIO3 (Pin 5)

Install dependencies:
  pip install luma.oled pimoroni-bme280 smbus2

Enable I2C on Pi:
  sudo raspi-config -> Interface Options -> I2C -> Enable

Usage:
  python ssd1306_demo.py              # Show BME280 readings
  python ssd1306_demo.py --graphics_demo  # Show shapes demo
"""

import argparse
from time import sleep

from bme280 import BME280
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306
from smbus2 import SMBus


def graphics_demo(device):
    """Show shapes and fills demo."""
    print("Running graphics demo...")

    # Demo 1: Simple text
    with canvas(device) as draw:
        draw.text((0, 0), "Hello!", fill="white")
        draw.text((0, 16), "SSD1306 OLED", fill="white")
        draw.text((0, 32), "128x64 pixels", fill="white")

    sleep(3)

    # Demo 2: Shapes
    with canvas(device) as draw:
        draw.rectangle((0, 0, 127, 63), outline="white")  # Border
        draw.ellipse((10, 10, 50, 50), outline="white")   # Circle
        draw.line((60, 10, 120, 50), fill="white")        # Diagonal line

    sleep(3)

    # Demo 3: Filled shapes
    with canvas(device) as draw:
        draw.rectangle((10, 10, 60, 30), fill="white")
        draw.text((70, 15), "Filled", fill="white")

    sleep(3)

    device.clear()
    print("Graphics demo complete.")


def sensor_display(device):
    """Show BME280 sensor readings, updating every 500ms."""
    print("Initializing BME280 sensor...")

    bus = SMBus(1)
    bme = BME280(i2c_dev=bus)

    # Flush first junk reading
    bme.get_temperature()
    sleep(1.0)

    print("Displaying sensor readings (Ctrl+C to exit)...")

    try:
        while True:
            temp_c = bme.get_temperature()
            temp_f = temp_c * 9 / 5 + 32
            pressure = bme.get_pressure()
            humidity = bme.get_humidity()

            with canvas(device) as draw:
                # Title
                draw.text((0, 0), "BME280 Sensor", fill="white")
                draw.line((0, 12, 127, 12), fill="white")

                # Readings
                draw.text((0, 18), f"Temp: {temp_f:.1f} F", fill="white")
                draw.text((0, 32), f"Pres: {pressure:.1f} hPa", fill="white")
                draw.text((0, 46), f"Hum:  {humidity:.1f} %", fill="white")

            sleep(0.5)

    except KeyboardInterrupt:
        print("\nStopping...")
        device.clear()


def main():
    parser = argparse.ArgumentParser(description="SSD1306 OLED Display Demo")
    parser.add_argument(
        "--graphics_demo",
        action="store_true",
        help="Run shapes and fills demo instead of sensor display",
    )
    args = parser.parse_args()

    # Initialize I2C interface (default address 0x3C, bus 1)
    serial = i2c(port=1, address=0x3C)
    device = ssd1306(serial)

    print("Display initialized.")

    if args.graphics_demo:
        graphics_demo(device)
    else:
        sensor_display(device)


if __name__ == "__main__":
    main()
