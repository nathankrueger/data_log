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

# Configuration
RADIO_FREQ_MHZ = 915.0  # Use 868.0 for EU, 915.0 for US
CS_PIN = board.D24  # Chip select (GPIO 24) - use a regular GPIO, not CE0/CE1
RESET_PIN = board.D25  # Reset (GPIO 25)


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


def receive_messages(rfm9x):
    """Listen for incoming messages."""
    print("Waiting for messages...")
    while True:
        packet = rfm9x.receive(timeout=5.0)
        if packet is not None:
            try:
                message = packet.decode("utf-8")
                rssi = rfm9x.last_rssi
                print(f"Received: {message} (RSSI: {rssi} dB)")
            except UnicodeDecodeError:
                print(f"Received raw bytes: {packet}")
        else:
            print("No message received, still listening...")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 radio.py [send|receive]")
        sys.exit(1)

    mode = sys.argv[1].lower()
    rfm9x = setup_radio()

    if mode == "send":
        send_messages(rfm9x)
    elif mode == "receive":
        receive_messages(rfm9x)
    else:
        print(f"Unknown mode: {mode}")
        print("Use 'send' or 'receive'")
        sys.exit(1)


if __name__ == "__main__":
    main()
