#!/usr/bin/env python3
"""
Diagnostic script for SX1262 radio issues.

This script helps debug hardware and configuration problems with the SX1262 radio.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radio.sx1262_driver import SX1262Driver


def test_spi_communication(driver: SX1262Driver) -> bool:
    """Test basic SPI communication."""
    print("\n1. Testing SPI communication...")
    try:
        status = driver.get_status()
        print(f"   ✓ Status register read: 0x{status:02x}")
        return status != 0
    except Exception as e:
        print(f"   ✗ Failed to read status: {e}")
        return False


def test_gpio_pins(driver: SX1262Driver) -> bool:
    """Test GPIO pin configuration."""
    print("\n2. Testing GPIO pins...")
    try:
        print(f"   BUSY pin active: {driver._busy.is_active}")
        print(f"   DIO1 pin active: {driver._dio1.is_active}")
        if driver._txen:
            print(f"   TXEN configured: GPIO {driver._txen.pin}")
        if driver._rxen:
            print(f"   RXEN configured: GPIO {driver._rxen.pin}")
        print("   ✓ GPIO pins accessible")
        return True
    except Exception as e:
        print(f"   ✗ GPIO error: {e}")
        return False


def test_rf_switch(driver: SX1262Driver) -> bool:
    """Test RF switch control."""
    print("\n3. Testing RF switch control...")
    try:
        print("   Setting RX mode...")
        driver.set_rf_switch_rx()
        time.sleep(0.1)
        
        print("   Setting TX mode...")
        driver.set_rf_switch_tx()
        time.sleep(0.1)
        
        print("   Setting OFF mode...")
        driver.set_rf_switch_off()
        time.sleep(0.1)
        
        print("   ✓ RF switch control working")
        return True
    except Exception as e:
        print(f"   ✗ RF switch error: {e}")
        return False


def test_configuration(driver: SX1262Driver) -> bool:
    """Test radio configuration."""
    print("\n4. Testing radio configuration...")
    try:
        driver.configure(
            frequency_hz=915_000_000,
            tx_power=22,
            spreading_factor=SX1262Driver.SF7,
            bandwidth=SX1262Driver.BW_125000,
            coding_rate=SX1262Driver.CR_4_5,
            sync_word=0x1424,
            preamble_len=8,
        )
        print("   ✓ Configuration applied successfully")
        return True
    except Exception as e:
        print(f"   ✗ Configuration error: {e}")
        return False


def test_irq_status(driver: SX1262Driver) -> bool:
    """Test IRQ status reading."""
    print("\n5. Testing IRQ status...")
    try:
        irq = driver.get_irq_status()
        print(f"   IRQ status: 0x{irq:04x}")
        
        # Clear IRQ
        driver.clear_irq_status()
        irq = driver.get_irq_status()
        print(f"   IRQ after clear: 0x{irq:04x}")
        print("   ✓ IRQ status working")
        return True
    except Exception as e:
        print(f"   ✗ IRQ error: {e}")
        return False


def test_send_operation(driver: SX1262Driver) -> bool:
    """Test a simple send operation."""
    print("\n6. Testing send operation...")
    try:
        test_data = b"TEST"
        print(f"   Sending {len(test_data)} bytes: {test_data}")
        
        success = driver.send(test_data, timeout_ms=2000)
        if success:
            print("   ✓ Send completed successfully")
            return True
        else:
            print("   ✗ Send operation failed (no TX_DONE)")
            
            # Check IRQ status for clues
            irq = driver.get_irq_status()
            print(f"   Final IRQ status: 0x{irq:04x}")
            
            if irq & SX1262Driver.IRQ_TIMEOUT:
                print("   ! TX timeout occurred")
            if irq & SX1262Driver.IRQ_TX_DONE:
                print("   ! TX_DONE flag is set (should have succeeded)")
            
            return False
    except Exception as e:
        print(f"   ✗ Send error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_receive_mode(driver: SX1262Driver) -> bool:
    """Test entering receive mode."""
    print("\n7. Testing receive mode...")
    try:
        print("   Entering RX mode (2 second timeout)...")
        result = driver.receive(timeout_ms=2000)
        
        if result is None:
            print("   ✓ RX mode entered (timeout as expected, no packets)")
        else:
            data, rssi, snr = result
            print(f"   ✓ Received packet: {data} (RSSI: {rssi}, SNR: {snr})")
        
        return True
    except Exception as e:
        print(f"   ✗ Receive error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="SX1262 Radio Diagnostic Tool")
    parser.add_argument("--spi-bus", type=int, default=0, help="SPI bus number")
    parser.add_argument("--spi-cs", type=int, default=0, help="SPI chip select")
    parser.add_argument("--reset-pin", type=int, default=22, help="Reset GPIO pin")
    parser.add_argument("--busy-pin", type=int, default=18, help="Busy GPIO pin")
    parser.add_argument("--dio1-pin", type=int, default=16, help="DIO1 GPIO pin")
    parser.add_argument("--txen-pin", type=int, default=6, help="TX enable GPIO pin")
    parser.add_argument("--rxen-pin", type=int, default=5, help="RX enable GPIO pin")
    parser.add_argument("--rfm9x-compatible", action="store_true", 
                        help="Enable RFM9x compatibility mode")
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("SX1262 Radio Diagnostic Tool")
    print("=" * 60)
    
    print(f"\nConfiguration:")
    print(f"  SPI: bus={args.spi_bus}, cs={args.spi_cs}")
    print(f"  GPIO: reset={args.reset_pin}, busy={args.busy_pin}, dio1={args.dio1_pin}")
    print(f"  RF Switch: txen={args.txen_pin}, rxen={args.rxen_pin}")
    print(f"  RFM9x compatible: {args.rfm9x_compatible}")
    
    # Create driver
    try:
        driver = SX1262Driver(
            spi_bus=args.spi_bus,
            spi_cs=args.spi_cs,
            reset_pin=args.reset_pin,
            busy_pin=args.busy_pin,
            dio1_pin=args.dio1_pin,
            txen_pin=args.txen_pin,
            rxen_pin=args.rxen_pin,
            rfm9x_compatible=args.rfm9x_compatible,
        )
    except Exception as e:
        print(f"\n✗ Failed to create driver: {e}")
        return 1
    
    # Initialize
    print("\nInitializing radio...")
    try:
        if not driver.begin():
            print("✗ Radio initialization failed")
            return 1
        print("✓ Radio initialized")
    except Exception as e:
        print(f"✗ Initialization error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    # Run tests
    results = []
    results.append(("SPI Communication", test_spi_communication(driver)))
    results.append(("GPIO Pins", test_gpio_pins(driver)))
    results.append(("RF Switch", test_rf_switch(driver)))
    results.append(("Configuration", test_configuration(driver)))
    results.append(("IRQ Status", test_irq_status(driver)))
    results.append(("Send Operation", test_send_operation(driver)))
    results.append(("Receive Mode", test_receive_mode(driver)))
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    for test_name, passed in results:
        status = "PASS" if passed else "FAIL"
        symbol = "✓" if passed else "✗"
        print(f"{symbol} {test_name}: {status}")
    
    passed = sum(1 for _, p in results if p)
    total = len(results)
    print(f"\nTotal: {passed}/{total} tests passed")
    
    # Cleanup
    try:
        driver.close()
    except Exception:
        pass
    
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
