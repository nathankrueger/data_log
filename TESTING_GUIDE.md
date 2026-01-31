# SX1262 ↔ RFM9x Testing Guide

## Quick Diagnostic

First, verify SX1262 basic operation:

```bash
python scripts/radio_debug.py --rfm9x-compatible
```

Expected output: All tests should PASS, especially "Send Operation".

## Test Scenarios

### Scenario 1: SX1262 → RFM9x (SX1262 sending)

**Pi with SX1262 (sender):**
```bash
python scripts/radio_test.py -s -t sx1262 --no-led --no-sensor
```

**Pi with RFM9x (receiver):**
```bash
python scripts/radio_test.py -r -t rfm9x --no-led --no-sensor
```

**Expected Result:**
- SX1262 should show "Sending: Counter: X" (no "Send failed!")
- RFM9x should receive and display packets with RSSI

---

### Scenario 2: RFM9x → SX1262 (RFM9x sending)

**Pi with RFM9x (sender):**
```bash
python scripts/radio_test.py -s -t rfm9x --no-led --no-sensor
```

**Pi with SX1262 (receiver):**
```bash
python scripts/radio_test.py -r -t sx1262 --no-led --no-sensor
```

**Expected Result:**
- RFM9x should show "Sending: Counter: X"
- SX1262 should receive and display packets with RSSI

---

### Scenario 3: SX1262 ↔ SX1262 (Native Mode)

Only use this if you have two SX1262 radios and want to test them together without RFM9x compatibility mode.

**First Pi with SX1262 (sender):**
```bash
python scripts/radio_test.py -s -t sx1262 --sx126x-native --no-led --no-sensor
```

**Second Pi with SX1262 (receiver):**
```bash
python scripts/radio_test.py -r -t sx1262 --sx126x-native --no-led --no-sensor
```

**Expected Result:**
- Sender shows "Sending: Counter: X"
- Receiver shows packets with RSSI

---

## Troubleshooting

### "Send failed!" on SX1262

**Cause**: DIO2 RF switch conflict (should be fixed now)

**Check**:
1. Run diagnostic: `python scripts/radio_debug.py --rfm9x-compatible`
2. Look for "Send Operation: PASS"
3. If still failing, check IRQ status output

**Fixes**:
- Verify TXEN (GPIO 6) and RXEN (GPIO 5) are properly wired
- Check power supply (SX1262 needs stable 3.3V)
- Verify SPI connection (run diagnostic test)

---

### SX1262 Not Receiving from RFM9x

**Cause**: IQ inversion mismatch or RF switch issues

**Check**:
1. Verify `rfm9x_compatible=True` (default in radio_test.py)
2. Run diagnostic to ensure RX mode works: `python scripts/radio_debug.py --rfm9x-compatible`
3. Check if RXEN pin is properly connected and functioning

**Debug**:
```bash
# On SX1262 receiver, add verbose logging
python scripts/radio_test.py -r -t sx1262 --no-led --no-sensor
```

Check for:
- "Waiting for messages..." appears
- No error messages during receive
- Radio stays in RX mode

---

### RFM9x Not Receiving from SX1262

**Cause**: Usually power or frequency mismatch

**Check**:
1. Both radios on same frequency (915.0 MHz default)
2. SX1262 is actually transmitting (no "Send failed!")
3. Distance between radios (start with 1-2 meters)

**Debug**:
```bash
# Verify SX1262 is transmitting
python scripts/radio_debug.py --rfm9x-compatible
# Look for "Send Operation: PASS"
```

---

### No Communication at All

**Check in order**:

1. **Power**: Both radios powered properly?
   ```bash
   # Check SX1262 voltage on 3V3 pin
   # Should be 3.0-3.6V
   ```

2. **SPI**: SX1262 SPI working?
   ```bash
   python scripts/radio_debug.py --rfm9x-compatible
   # Check "SPI Communication: PASS"
   ```

3. **Frequency**: Both on same frequency?
   ```bash
   # Default is 915.0 MHz (US)
   # EU uses 868.0 MHz
   ```

4. **LoRa Parameters**: Must match exactly:
   - Spreading Factor: 7 (default)
   - Bandwidth: 125 kHz (default)
   - Coding Rate: 4/5 (default)
   - Sync Word: 0x12 for RFM9x, 0x1424 for SX1262 (compatible)

5. **Antennas**: Connected and not damaged?

6. **Distance**: Start with radios close (1-2 meters)

---

## Understanding the Output

### Sender Output (Good)
```
Sending: Counter: 0
Sending: Counter: 1
Sending: Counter: 2
```

### Sender Output (Bad)
```
Sending: Counter: 0
  -> Send failed!
```

### Receiver Output (Good)
```
Received: b'Counter: 0'
RSSI: -45 dBm
```

### Receiver Output (No Packets)
```
Waiting for messages... (Ctrl+C to stop)

[No output = no packets received]
```

---

## RSSI Reference

| RSSI Range | Signal Quality | Notes |
|------------|----------------|-------|
| -30 to -60 | Excellent | Very close range |
| -60 to -80 | Good | Normal operating range |
| -80 to -100 | Fair | Near maximum range |
| -100 to -120 | Poor | Unreliable, at limits |

---

## Configuration Matrix

| Parameter | RFM9x Default | SX1262 Default | Must Match? |
|-----------|---------------|----------------|-------------|
| Frequency | 915.0 MHz | 915.0 MHz | ✅ YES |
| SF | 7 | 7 | ✅ YES |
| Bandwidth | 125 kHz | 125 kHz | ✅ YES |
| Coding Rate | 4/5 | 4/5 | ✅ YES |
| Sync Word | 0x12 | 0x1424 | ✅ Compatible |
| Preamble | 8 | 8 | ✅ YES |
| TX Power | 23 dBm | 22 dBm | ❌ No (max differs) |
| IQ Inversion | N/A | Auto-handled | ✅ Automatic |

---

## Advanced: Checking RF Switch

To manually verify RF switch operation:

```python
from radio.sx1262_driver import SX1262Driver
import time

driver = SX1262Driver(rfm9x_compatible=True)
driver.begin()

# Test RX mode
print("Setting RX mode...")
driver.set_rf_switch_rx()
time.sleep(1)

# Test TX mode
print("Setting TX mode...")
driver.set_rf_switch_tx()
time.sleep(1)

# Back to RX
print("Back to RX mode...")
driver.set_rf_switch_rx()

driver.close()
```

Monitor GPIO pins:
- **GPIO 5 (RXEN)**: Should be HIGH in RX, LOW in TX
- **GPIO 6 (TXEN)**: Should be LOW in RX, HIGH in TX

---

## Success Criteria

✅ **Full Success**: Both directions working
- SX1262 sends → RFM9x receives
- RFM9x sends → SX1262 receives
- No "Send failed!" errors
- RSSI values reasonable (-30 to -100 dBm)

🟨 **Partial Success**: One direction working
- Indicates asymmetric issue (power, antenna, etc.)
- Check the non-working direction specifically

❌ **No Success**: Neither direction working
- Check power, wiring, SPI communication
- Run diagnostic tool
- Verify hardware with known-good radio

---

## Getting Help

If issues persist after following this guide:

1. **Run full diagnostic**:
   ```bash
   python scripts/radio_debug.py --rfm9x-compatible > sx1262_diag.txt
   ```

2. **Capture test output**:
   ```bash
   python scripts/radio_test.py -s -t sx1262 --no-led --no-sensor > sx1262_send.txt
   python scripts/radio_test.py -r -t sx1262 --no-led --no-sensor > sx1262_recv.txt
   ```

3. **Check hardware**:
   - Verify wiring matches pinout in `_assets/ws_sx1262_pinout.png`
   - Measure voltages on VCC, GND
   - Check continuity on SPI lines
   - Verify antennas connected

4. **Provide information**:
   - Which scenario fails (1, 2, or 3)?
   - Diagnostic output
   - Test output from both sender and receiver
   - Photo of hardware setup (if possible)
