#!/usr/bin/env python3
"""
Diagnostic script for SX1262 radio - tests with custom spidev/gpiozero driver.
Run this on the Pi with the SX1262 to verify communication.
"""

import atexit
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Global reference for cleanup
_driver = None
_cleanup_in_progress = False

# Pin configuration - adjust these to match your wiring
RESET_PIN = 22
BUSY_PIN = 18
DIO1_PIN = 16
TXEN_PIN = 6
RXEN_PIN = 5
SPI_BUS = 0
SPI_CS = 0

FREQUENCY = 915  # MHz


def _cleanup():
    """Global cleanup handler."""
    global _driver, _cleanup_in_progress
    if _cleanup_in_progress:
        return
    _cleanup_in_progress = True

    if _driver is not None:
        try:
            _driver.close()
        except Exception:
            pass
        _driver = None
    _cleanup_in_progress = False


def _signal_handler(signum, frame):
    """Handle termination signals."""
    global _cleanup_in_progress
    if _cleanup_in_progress:
        print("\nForce exit...")
        os._exit(1)
    print("\nSignal received, cleaning up...")
    _cleanup()
    sys.exit(0)


# Register cleanup handlers
atexit.register(_cleanup)
signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def main():
    global _driver

    print("=== SX1262 Diagnostic Script (Custom Driver) ===\n")

    print("1. Checking SPI devices...")
    spi_devices = [f for f in os.listdir('/dev') if f.startswith('spi')]
    if spi_devices:
        print(f"   Found: {spi_devices}")
    else:
        print("   ERROR: No SPI devices found! Enable SPI with raspi-config")
        return

    # Test SPI directly
    print("\n2. Testing raw SPI communication...")
    try:
        import spidev
        spi = spidev.SpiDev()
        spi.open(SPI_BUS, SPI_CS)
        spi.max_speed_hz = 2000000
        spi.mode = 0
        response = spi.xfer2([0xC0, 0x00])  # GetStatus command
        print(f"   SPI test response: {response}")
        spi.close()
        if response == [0, 0]:
            print("   WARNING: Got all zeros - check wiring!")
    except Exception as e:
        print(f"   SPI test failed: {e}")
        return

    print("\n3. Importing custom SX1262 driver...")
    try:
        from radio.sx1262_driver import SX1262Driver
        print("   Import successful")
    except ImportError as e:
        print(f"   FAILED: {e}")
        return

    print("\n4. Creating SX1262Driver instance...")
    print(f"   SPI: bus={SPI_BUS}, cs={SPI_CS}")
    print(f"   GPIO: RESET={RESET_PIN}, BUSY={BUSY_PIN}, DIO1={DIO1_PIN}")
    print(f"   RF Switch: TXEN={TXEN_PIN}, RXEN={RXEN_PIN}")

    try:
        _driver = SX1262Driver(
            spi_bus=SPI_BUS,
            spi_cs=SPI_CS,
            reset_pin=RESET_PIN,
            busy_pin=BUSY_PIN,
            dio1_pin=DIO1_PIN,
            txen_pin=TXEN_PIN,
            rxen_pin=RXEN_PIN,
        )
        print("   Driver created successfully")
    except Exception as e:
        print(f"   FAILED to create driver: {e}")
        return

    print("\n5. Initializing radio (begin)...")
    try:
        result = _driver.begin()
        print(f"   Result: {result}")
        if not result:
            print("   FAILED to initialize radio!")
            print("   Check wiring and power.")
            return
    except Exception as e:
        print(f"   FAILED: {type(e).__name__}: {e}")
        return

    print(f"\n6. Configuring radio...")
    print(f"   Frequency: {FREQUENCY} MHz")
    print(f"   SF: 7, BW: 125kHz, CR: 4/5")
    print(f"   Sync word: 0x1424 (RFM9x compatible)")
    try:
        _driver.configure(
            frequency_hz=FREQUENCY * 1_000_000,
            tx_power=22,
            spreading_factor=SX1262Driver.SF7,
            bandwidth=SX1262Driver.BW_125000,
            coding_rate=SX1262Driver.CR_4_5,
            sync_word=0x1424,
            preamble_len=8,
        )
        print("   Configuration complete")
    except Exception as e:
        print(f"   FAILED: {e}")
        return

    print("\n7. Getting radio status...")
    try:
        status = _driver.get_status()
        print(f"   Status: 0x{status:02X}")
        # Decode status bits
        chip_mode = (status >> 4) & 0x07
        cmd_status = (status >> 1) & 0x07
        modes = {0: "Unused", 1: "RFU", 2: "STBY_RC", 3: "STBY_XOSC",
                 4: "FS", 5: "RX", 6: "TX"}
        print(f"   Chip mode: {modes.get(chip_mode, 'Unknown')} ({chip_mode})")
        print(f"   Command status: {cmd_status}")
    except Exception as e:
        print(f"   Could not get status: {e}")

    print("\n=== Radio Initialized Successfully! ===")
    print("\n=== Testing TX/RX ===\n")

    # Test sending
    print("8. Testing TX (sending test packet)...")
    try:
        test_msg = b"Hello from SX1262!"
        result = _driver.send(test_msg)
        print(f"   Send result: {result}")
        if result:
            print(f"   Sent: {test_msg.decode()}")
        else:
            print("   Send failed or timed out")
    except Exception as e:
        print(f"   TX error: {e}")

    print("\n9. Testing RX (listening for 10 seconds)...")
    print("   Waiting for packets... (Ctrl+C to stop)")

    try:
        start_time = time.monotonic()
        packet_count = 0

        while (time.monotonic() - start_time) < 10:
            result = _driver.receive(timeout_ms=1000)
            if result is not None:
                data, rssi, snr = result
                packet_count += 1
                try:
                    message = data.decode('utf-8')
                except UnicodeDecodeError:
                    message = f"<raw: {data!r}>"
                print(f"\n   *** RECEIVED PACKET #{packet_count} ***")
                print(f"   Message: {message}")
                print(f"   RSSI: {rssi} dBm, SNR: {snr} dB")
                print(f"   Length: {len(data)} bytes")
            else:
                elapsed = int(time.monotonic() - start_time)
                print(f"   Listening... ({elapsed}s)", end='\r')

        print(f"\n   Received {packet_count} packets in 10 seconds")

    except KeyboardInterrupt:
        print("\n\n   Stopped by user")

    print("\nCleaning up...")
    _cleanup()
    print("Done!")


if __name__ == "__main__":
    main()
