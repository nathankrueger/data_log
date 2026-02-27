#!/usr/bin/env python3
"""
Listen for LoRa packets using an RFM9x breakout connected via FT232H USB-to-SPI
adapter. Works on macOS, Linux, and Raspberry Pi 5 — no native SPI/GPIO needed.

Wiring (RFM9x breakout to FT232H):
    RFM9x       FT232H          Blinka name
    ------      ------          -----------
    VIN  -----> +3.3V           (power)
    GND  -----> GND             (power)
    SCK  -----> AD0  (SCLK)    board.SCK
    MOSI -----> AD1  (DO)      board.MOSI
    MISO -----> AD2  (DI)      board.MISO
    CS   -----> AD4  (GPIO)    board.D4
    RST  -----> AD5  (GPIO)    board.D5

    Note: AD3 is the FT232H hardware CS — avoid using it.
    AD4-AD7 and AC0-AC9 are free GPIOs for CS/RST.

Prerequisites:
    macOS:
        brew install libusb
        pip install pyftdi adafruit-blinka adafruit-circuitpython-rfm9x

    Raspberry Pi 5 / Linux:
        sudo apt install libusb-1.0-0
        pip install pyftdi adafruit-blinka adafruit-circuitpython-rfm9x

    Windows (WSL):
        sudo apt install libusb-1.0-0
        pip install pyftdi adafruit-blinka adafruit-circuitpython-rfm9x

        Attach the FT232H USB device to WSL:
            (PowerShell, admin) usbipd list
            (PowerShell, admin) usbipd bind --busid <BUSID>
            (PowerShell, admin) usbipd attach --wsl --busid <BUSID>

        FTDI Permissions:
            sudo tee /etc/udev/rules.d/99-ftdi.rules << 'EOF'
            SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6014", MODE="0666"
            EOF

            sudo udevadm control --reload-rules && sudo udevadm trigger

Usage:
    export BLINKA_FT232H=1
    python3 rfm9x_ft232h_listen.py
    python3 rfm9x_ft232h_listen.py --freq 915.5 --sf 9 --bw 125000
"""

import argparse
import os

import board
import busio
import digitalio

os.environ['BLINKA_FT232H'] = '1'
import adafruit_rfm9x


def main():
    parser = argparse.ArgumentParser(
        description="Listen for LoRa packets via FT232H + RFM9x."
    )
    parser.add_argument("--freq", type=float, default=915.0,
                        help="Frequency in MHz (default: 915.0)")
    parser.add_argument("--sf", type=int, default=7,
                        help="Spreading factor 7-12 (default: 7)")
    parser.add_argument("--bw", type=int, default=125000,
                        help="Bandwidth in Hz: 125000, 250000, 500000 (default: 125000)")
    parser.add_argument("--cs", default="D4",
                        help="FT232H CS pin (default: D4)")
    parser.add_argument("--rst", default="D5",
                        help="FT232H RST pin (default: D5)")
    args = parser.parse_args()

    cs_pin = getattr(board, args.cs)
    rst_pin = getattr(board, args.rst)

    spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)
    cs = digitalio.DigitalInOut(cs_pin)
    reset = digitalio.DigitalInOut(rst_pin)

    rfm9x = adafruit_rfm9x.RFM9x(spi, cs, reset, args.freq)
    rfm9x.spreading_factor = args.sf
    rfm9x.signal_bandwidth = args.bw
    rfm9x.coding_rate = 5
    rfm9x.preamble_length = 8
    rfm9x.enable_crc = True

    backend = "FT232H" if os.environ.get("BLINKA_FT232H") else "native"
    print(f"Listening on {args.freq} MHz  SF={args.sf}  BW={args.bw} Hz  ({backend})")
    print("Ctrl+C to stop.\n")

    try:
        while True:
            packet = rfm9x.receive(timeout=5.0)
            if packet is not None:
                rssi = rfm9x.last_rssi
                try:
                    text = packet.decode("utf-8")
                    print(f"[RSSI {rssi:>4d} dBm] {text}")
                except UnicodeDecodeError:
                    print(f"[RSSI {rssi:>4d} dBm] (raw) {packet.hex()}")
            else:
                print("... no packet")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        spi.deinit()


if __name__ == "__main__":
    main()
