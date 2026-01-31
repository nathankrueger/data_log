#!/usr/bin/env python3
"""
Diagnostic script for SX1262 radio - tests receive with verbose output.
Run this on the Pi with the SX1262 to debug communication issues.
"""

import atexit
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Global reference for cleanup
_lora = None

# Pin configuration - adjust these to match your wiring
RESET_PIN = 22
BUSY_PIN = 18
DIO1_PIN = 16
TXEN_PIN = 6
RXEN_PIN = 5
SPI_BUS = 0
SPI_CS = 0

FREQUENCY = 915  # MHz

ALL_PINS = [RESET_PIN, BUSY_PIN, DIO1_PIN, TXEN_PIN, RXEN_PIN]


def release_gpio_resources():
    """Release any GPIO resources held from a previous run."""
    released = False

    # Try gpiod first (what LoRaRF library may use internally)
    try:
        import gpiod
        chip = gpiod.Chip("/dev/gpiochip0")
        print(f"   gpiod: Chip {chip.name} opened")
        # Just opening and closing the chip can help reset state
        chip.close()
        released = True
    except Exception as e:
        print(f"   gpiod: {e}")

    # Also try lgpio
    try:
        import lgpio
        h = lgpio.gpiochip_open(0)

        for pin in ALL_PINS:
            try:
                lgpio.gpio_free(h, pin)
            except:
                pass  # Pin wasn't claimed, that's fine

        lgpio.gpiochip_close(h)
        released = True
    except Exception as e:
        print(f"   lgpio: {e}")

    return released


def hardware_reset():
    """Perform hardware reset of the radio."""
    try:
        import lgpio
        h = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(h, RESET_PIN)

        # Pulse reset low for 1ms, then high
        lgpio.gpio_write(h, RESET_PIN, 0)
        time.sleep(0.001)
        lgpio.gpio_write(h, RESET_PIN, 1)
        time.sleep(0.01)

        lgpio.gpio_free(h, RESET_PIN)
        lgpio.gpiochip_close(h)
        return True
    except Exception as e:
        print(f"   Note: Hardware reset failed: {e}")
        return False


def cleanup_lora(lora_obj):
    """Properly clean up LoRa library resources."""
    if lora_obj is None:
        return

    # Try to close internal SPI handle
    try:
        if hasattr(lora_obj, '_spi') and lora_obj._spi is not None:
            if hasattr(lora_obj._spi, 'close'):
                lora_obj._spi.close()
    except Exception:
        pass

    # Try to close internal GPIO handles
    for attr in ['_gpio', '_cs', '_reset', '_busy', '_irq', '_txen', '_rxen']:
        try:
            obj = getattr(lora_obj, attr, None)
            if obj is not None and hasattr(obj, 'close'):
                obj.close()
        except Exception:
            pass

    # Call library's end/sleep method
    try:
        if hasattr(lora_obj, 'end'):
            lora_obj.end()
        elif hasattr(lora_obj, 'sleep'):
            lora_obj.sleep()
    except Exception:
        pass


def _cleanup():
    """Global cleanup handler."""
    global _lora
    if _lora is not None:
        cleanup_lora(_lora)
        _lora = None
    release_gpio_resources()


def _signal_handler(signum, frame):
    """Handle termination signals."""
    print("\nSignal received, cleaning up...")
    _cleanup()
    sys.exit(0)


# Register cleanup handlers
atexit.register(_cleanup)
signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def main():
    global _lora

    print("=== SX1262 Diagnostic Script ===\n")

    print("1. Checking SPI devices...")
    import os
    spi_devices = [f for f in os.listdir('/dev') if f.startswith('spi')]
    if spi_devices:
        print(f"   Found: {spi_devices}")
    else:
        print("   ERROR: No SPI devices found! Enable SPI with raspi-config")

    print("\n2. Releasing any held GPIO resources...")
    release_gpio_resources()
    print("   Done")

    print("\n3. Performing hardware reset...")
    if hardware_reset():
        print("   Reset complete")
    else:
        print("   Skipped (will rely on library reset)")

    print("\n4. Importing LoRaRF library...")
    try:
        from LoRaRF import SX126x
        print("   Import successful")
    except ImportError as e:
        print(f"   FAILED: {e}")
        print("   Install with: pip install LoRaRF")
        return

    print("\n5. Creating SX126x instance...")
    _lora = SX126x()

    print(f"\n6. Configuring SPI (bus={SPI_BUS}, cs={SPI_CS})...")
    _lora.setSpi(SPI_BUS, SPI_CS)

    print(f"\n7. Configuring GPIO pins...")
    print(f"   RESET={RESET_PIN}, BUSY={BUSY_PIN}, DIO1={DIO1_PIN}, TXEN={TXEN_PIN}, RXEN={RXEN_PIN}")
    _lora.setPins(RESET_PIN, BUSY_PIN, DIO1_PIN, TXEN_PIN, RXEN_PIN)

    print("\n8. Calling begin() (with retry)...")
    for attempt in range(3):
        result = _lora.begin()
        print(f"   Attempt {attempt + 1}: {result}")
        if result:
            break
        print("   Retrying in 1 second...")
        time.sleep(1)

    if not result:
        print("\n   FAILED to initialize radio after 3 attempts!")
        print("   Possible issues:")
        print("   - Wrong GPIO wiring")
        print("   - SPI not enabled (run raspi-config)")
        print("   - Radio module not powered")
        print("   - Another process holding GPIO resources")
        print("\n   Try rebooting the Pi and running again.")
        return

    print(f"\n7. Setting frequency to {FREQUENCY} MHz...")
    _lora.setFrequency(FREQUENCY * 1_000_000)

    print("\n8. Setting LoRa modulation (SF=7, BW=125kHz, CR=4/5)...")
    # SF=7, BW index 7 = 125kHz, CR index 1 = 4/5
    _lora.setLoRaModulation(7, 7, 1)

    print("\n9. Setting packet params (preamble=8, explicit header, max=255, CRC=on)...")
    _lora.setLoRaPacket(8, 0, 255, 1, 0)

    print("\n10. Setting sync word to 0x1424 (RFM9x compatible)...")
    _lora.setSyncWord(0x1424)

    print("\n=== Configuration Complete ===")
    print(f"Frequency: {FREQUENCY} MHz")
    print("SF: 7, BW: 125kHz, CR: 4/5")
    print("Sync word: 0x1424 (matches RFM9x 0x12)")
    print("\n=== Waiting for packets (Ctrl+C to stop) ===\n")

    # Put radio in continuous receive mode
    print("Starting continuous receive mode...")
    _lora.request(0xFFFFFF)  # Continuous receive (no timeout)

    packet_count = 0
    poll_count = 0
    try:
        while True:
            poll_count += 1
            # Poll for received data using status()
            status = _lora.status()

            # Status: 0 = waiting, 1 = received, 2 = transmitted, -1 = error
            if status == 1:  # Packet received
                packet_count += 1
                data = []
                while _lora.available():
                    data.append(_lora.read())

                rssi = _lora.packetRssi()
                snr = _lora.snr()

                try:
                    message = bytes(data).decode('utf-8')
                except:
                    message = f"<raw: {bytes(data)!r}>"

                print(f"\n*** RECEIVED PACKET #{packet_count} ***")
                print(f"    Message: {message}")
                print(f"    RSSI: {rssi} dBm, SNR: {snr} dB")
                print(f"    Length: {len(data)} bytes\n")

                # Re-enter receive mode after getting a packet
                _lora.request(0xFFFFFF)

            elif poll_count % 100 == 0:
                # Print status every 100 polls (~1 second) to show we're alive
                print(f"  Polling... (status={status}, polls={poll_count})")

            time.sleep(0.01)  # 10ms poll interval

    except KeyboardInterrupt:
        print(f"\n\nStopping. Received {packet_count} packets total.")
    finally:
        print("Cleaning up...")
        cleanup_lora(_lora)
        release_gpio_resources()
        hardware_reset()
        print("Cleanup complete.")


if __name__ == "__main__":
    main()
