#!/usr/bin/env python3
"""
Simple LoRa radio communication using Adafruit RFM9x module on Raspberry Pi Zero 2W.

Wiring (RFM9x to Pi):
    VIN  -> 3.3V
    GND  -> GND
    SCK  -> GPIO 11 (SPI0 SCLK)
    MISO -> GPIO 9  (SPI0 MISO)
    MOSI -> GPIO 10 (SPI0 MOSI)
    CS   -> GPIO 8  (CE0) or GPIO 7 (CE1)
    RST  -> GPIO 25 (or any available GPIO)

Usage:
    python3 radio.py send       # Run on transmitter Pi
    python3 radio.py receive    # Run on receiver Pi

Requires:
    pip3 install adafruit-circuitpython-rfm9x
"""

import time
import sys
import board
import busio
import digitalio
import adafruit_rfm9x

from led import RgbLed

# Configuration
RADIO_FREQ_MHZ = 915.0  # Use 868.0 for EU, 915.0 for US
CS_PIN = board.D24  # Chip select (GPIO 24) - use a regular GPIO, not CE0/CE1
RESET_PIN = board.D25  # Reset (GPIO 25)

# RSSI to brightness mapping
RSSI_MAX = -50   # Strong signal -> full brightness (255)
RSSI_MIN = -100  # Weak signal (RFM9x sensitivity limit: -120) -> LED off (0)


def rssi_to_brightness(rssi: float) -> int:
    """Convert RSSI (dBm) to LED brightness (0-255)."""
    # Clamp RSSI to our range
    rssi = max(RSSI_MIN, min(RSSI_MAX, rssi))
    # Linear interpolation: -120 -> 0, -50 -> 255
    brightness = int((rssi - RSSI_MIN) / (RSSI_MAX - RSSI_MIN) * 255)
    return brightness


def setup_radio():
    """Initialize the RFM9x radio module."""
    spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)

    cs = digitalio.DigitalInOut(CS_PIN)
    reset = digitalio.DigitalInOut(RESET_PIN)

    rfm9x = adafruit_rfm9x.RFM9x(spi, cs, reset, RADIO_FREQ_MHZ)
    rfm9x.tx_power = 23  # Max power (range: 5-23 dBm)

    print(f"Radio initialized at {RADIO_FREQ_MHZ} MHz")
    return rfm9x


def send_messages(rfm9x):
    """Continuously send 'Hello World' messages."""
    counter = 0
    while True:
        message = f"Hello World #{counter}"
        print(f"Sending: {message}")
        rfm9x.send(bytes(message, "utf-8"))
        counter += 1
        time.sleep(2)


def receive_messages(rfm9x, led):
    """Listen for incoming messages."""
    print("Waiting for messages...")
    while True:
        packet = rfm9x.receive(timeout=5.0)
        if packet is not None:
            rssi = rfm9x.last_rssi
            brightness = rssi_to_brightness(rssi)
            led.flash(brightness, 0, 0, 0.5)  # Flash red, brightness based on RSSI
            try:
                message = packet.decode("utf-8")
                print(f"Received: {message} (RSSI: {rssi} dB, brightness: {brightness})")
            except UnicodeDecodeError:
                print(f"Received raw bytes: {packet} (RSSI: {rssi} dB)")
        else:
            print("No message received, still listening...")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 radio.py [send|receive]")
        sys.exit(1)

    mode = sys.argv[1].lower()
    rfm9x = setup_radio()
    led = None

    try:
        if mode == "send":
            send_messages(rfm9x)
        elif mode == "receive":
            led = RgbLed(red_bcm=17, green_bcm=27, blue_bcm=22, common_anode=True)
            receive_messages(rfm9x, led)
        else:
            print(f"Unknown mode: {mode}")
            print("Use 'send' or 'receive'")
            sys.exit(1)
    finally:
        if led:
            led.close()


if __name__ == "__main__":
    main()
