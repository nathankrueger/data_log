#!/usr/bin/env python3
"""
Diagnostic script for SX1262 radio - tests receive with verbose output.
Run this on the Pi with the SX1262 to debug communication issues.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Pin configuration - adjust these to match your wiring
RESET_PIN = 22
BUSY_PIN = 18
DIO1_PIN = 16
TXEN_PIN = 6
RXEN_PIN = 5
SPI_BUS = 0
SPI_CS = 0

FREQUENCY = 915  # MHz

def main():
    print("=== SX1262 Diagnostic Script ===\n")

    print("1. Checking SPI devices...")
    import os
    spi_devices = [f for f in os.listdir('/dev') if f.startswith('spi')]
    if spi_devices:
        print(f"   Found: {spi_devices}")
    else:
        print("   ERROR: No SPI devices found! Enable SPI with raspi-config")

    print("\n2. Importing LoRaRF library...")
    try:
        from LoRaRF import SX126x
        print("   Import successful")
    except ImportError as e:
        print(f"   FAILED: {e}")
        print("   Install with: pip install LoRaRF")
        return

    print("\n3. Creating SX126x instance...")
    lora = SX126x()

    print(f"\n4. Configuring SPI (bus={SPI_BUS}, cs={SPI_CS})...")
    lora.setSpi(SPI_BUS, SPI_CS)

    print(f"\n5. Configuring GPIO pins...")
    print(f"   RESET={RESET_PIN}, BUSY={BUSY_PIN}, DIO1={DIO1_PIN}, TXEN={TXEN_PIN}, RXEN={RXEN_PIN}")
    lora.setPins(RESET_PIN, BUSY_PIN, DIO1_PIN, TXEN_PIN, RXEN_PIN)

    print("\n6. Calling begin() (with retry)...")
    for attempt in range(3):
        result = lora.begin()
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
    lora.setFrequency(FREQUENCY * 1_000_000)

    print("\n8. Setting LoRa modulation (SF=7, BW=125kHz, CR=4/5)...")
    # SF=7, BW index 7 = 125kHz, CR index 1 = 4/5
    lora.setLoRaModulation(7, 7, 1)

    print("\n9. Setting packet params (preamble=8, explicit header, max=255, CRC=on)...")
    lora.setLoRaPacket(8, 0, 255, 1, 0)

    print("\n10. Setting sync word to 0x1424 (RFM9x compatible)...")
    lora.setSyncWord(0x1424)

    print("\n=== Configuration Complete ===")
    print(f"Frequency: {FREQUENCY} MHz")
    print("SF: 7, BW: 125kHz, CR: 4/5")
    print("Sync word: 0x1424 (matches RFM9x 0x12)")
    print("\n=== Waiting for packets (Ctrl+C to stop) ===\n")

    # Put radio in continuous receive mode
    print("Starting continuous receive mode...")
    lora.request(0xFFFFFF)  # Continuous receive (no timeout)

    packet_count = 0
    poll_count = 0
    try:
        while True:
            poll_count += 1
            # Poll for received data using status()
            status = lora.status()

            # Status: 0 = waiting, 1 = received, 2 = transmitted, -1 = error
            if status == 1:  # Packet received
                packet_count += 1
                data = []
                while lora.available():
                    data.append(lora.read())

                rssi = lora.packetRssi()
                snr = lora.snr()

                try:
                    message = bytes(data).decode('utf-8')
                except:
                    message = f"<raw: {bytes(data)!r}>"

                print(f"\n*** RECEIVED PACKET #{packet_count} ***")
                print(f"    Message: {message}")
                print(f"    RSSI: {rssi} dBm, SNR: {snr} dB")
                print(f"    Length: {len(data)} bytes\n")

                # Re-enter receive mode after getting a packet
                lora.request(0xFFFFFF)

            elif poll_count % 100 == 0:
                # Print status every 100 polls (~1 second) to show we're alive
                print(f"  Polling... (status={status}, polls={poll_count})")

            time.sleep(0.01)  # 10ms poll interval

    except KeyboardInterrupt:
        print(f"\n\nStopping. Received {packet_count} packets total.")
    finally:
        try:
            lora.sleep()
        except:
            pass


if __name__ == "__main__":
    main()
