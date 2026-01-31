# SX1262 Radio Fixes - January 31, 2026

## Problems Identified

### 1. "Send failed!" Error on SX1262
**Symptom**: SX1262 radio couldn't send packets - all sends returned failure.

**Root Cause**: DIO2 was configured as RF switch controller (`set_dio2_as_rf_switch(True)`), but the Waveshare Core1262-868M module uses dedicated TXEN/RXEN GPIO pins for RF switch control. This created a conflict where the hardware tried to control the RF switch in two different ways simultaneously, preventing proper TX operation.

### 2. RFM9x → SX1262 Communication Not Working
**Symptom**: SX1262 receiver couldn't receive packets from RFM9x transmitter.

**Root Cause**: Already fixed with IQ inversion (RX uses `invert_iq=1`), but RF switch timing and state management issues were preventing proper operation.

## Solutions Implemented

### 1. Disabled DIO2 RF Switch Control
**File**: `radio/sx1262_driver.py`

Changed in `begin()` method:
```python
# Before:
self.set_dio2_as_rf_switch(True)

# After:
self.set_dio2_as_rf_switch(False)  # Waveshare uses manual TXEN/RXEN
```

**Reason**: The Waveshare module has dedicated TXEN/RXEN pins that must be controlled manually via GPIO. Using DIO2 for RF switching conflicts with this design.

### 2. Added RF Switch Settling Delays
**Files**: `radio/sx1262_driver.py`

Added 1ms delays after RF switch changes to allow hardware to stabilize:

```python
def set_rf_switch_tx(self) -> None:
    """Set RF switch to TX mode."""
    if self._rxen:
        self._rxen.off()
    if self._txen:
        self._txen.on()
    time.sleep(0.001)  # 1ms for RF switch settling

def set_rf_switch_rx(self) -> None:
    """Set RF switch to RX mode."""
    if self._txen:
        self._txen.off()
    if self._rxen:
        self._rxen.on()
    time.sleep(0.001)  # 1ms for RF switch settling
```

**Reason**: RF switches need time to settle before radio operations begin.

### 3. Improved RF Switch State Management
**File**: `radio/sx1262_driver.py`

Changed in `send()` and `receive()` methods:

**Before**: Used `set_rf_switch_off()` after operations
**After**: Use `set_rf_switch_rx()` to keep radio in listening mode

This keeps the radio ready to receive packets immediately after sending or after receive operations complete.

### 4. Fixed Preamble Length Configuration
**File**: `radio/sx1262_driver.py`

**Before**: Hardcoded preamble length of 8 in send/receive operations
**After**: Store configured preamble length and use it consistently

```python
# In configure():
self._preamble_length = preamble_len  # Store for send/receive

# In send() and receive():
self.set_packet_params(self._preamble_length, 0, len(data), 1, 0)
```

**Reason**: Allows proper configuration and ensures consistency with RFM9x.

## IQ Inversion Configuration (Already Implemented)

For reference, the IQ inversion logic (already working):

```python
# In receive():
rx_invert_iq = 1 if self._rfm9x_compatible else 0
self.set_packet_params(self._preamble_length, 0, 255, 1, rx_invert_iq)

# In send():
# TX always uses standard IQ (invert_iq = 0)
self.set_packet_params(self._preamble_length, 0, len(data), 1, 0)
```

### IQ Inversion Rules

| Scenario | TX IQ | RX IQ | Notes |
|----------|-------|-------|-------|
| SX1262 ↔ RFM9x | 0 (standard) | 1 (inverted) | `rfm9x_compatible=True` (default) |
| SX1262 ↔ SX1262 | 0 (standard) | 0 (standard) | `rfm9x_compatible=False` or `--sx126x-native` |

## New Diagnostic Tool

Created `scripts/radio_debug.py` to help diagnose radio issues:

```bash
# Run diagnostics on SX1262
python scripts/radio_debug.py --rfm9x-compatible

# Test in SX1262-only mode
python scripts/radio_debug.py
```

The diagnostic tool tests:
1. SPI communication
2. GPIO pin configuration
3. RF switch control
4. Radio configuration
5. IRQ status handling
6. Send operation
7. Receive mode

## Testing Commands

### Test SX1262 Sending to RFM9x
```bash
# On Pi with SX1262 (sender)
python scripts/radio_test.py -s -t sx1262 --no-led --no-sensor

# On Pi with RFM9x (receiver)
python scripts/radio_test.py -r -t rfm9x --no-led --no-sensor
```

### Test RFM9x Sending to SX1262
```bash
# On Pi with RFM9x (sender)
python scripts/radio_test.py -s -t rfm9x --no-led --no-sensor

# On Pi with SX1262 (receiver)
python scripts/radio_test.py -r -t sx1262 --no-led --no-sensor
```

### Test SX1262 ↔ SX1262 (Native Mode)
```bash
# On first Pi with SX1262 (sender)
python scripts/radio_test.py -s -t sx1262 --sx126x-native --no-led --no-sensor

# On second Pi with SX1262 (receiver)
python scripts/radio_test.py -r -t sx1262 --sx126x-native --no-led --no-sensor
```

## Expected Behavior

After these fixes:
- ✅ SX1262 should successfully send packets (no more "Send failed!")
- ✅ RFM9x should receive packets from SX1262
- ✅ SX1262 should receive packets from RFM9x
- ✅ SX1262 ↔ SX1262 should work in native mode

## Technical Details

### Waveshare Core1262-868M RF Switch
The module uses an RF switch that requires:
- **TXEN = HIGH, RXEN = LOW** for transmit mode
- **TXEN = LOW, RXEN = HIGH** for receive mode
- **Both LOW** for standby/off mode

The DIO2-based RF switch control is for simpler modules that use DIO2 to control the RF switch. It doesn't apply to the Waveshare design.

### Why IQ Inversion?
SX126x and SX127x chip families use different default IQ polarities:
- **SX127x (RFM9x)**: Transmits with standard IQ polarity
- **SX126x (SX1262)**: Has configurable IQ inversion

For compatibility:
- SX1262 RX must invert IQ when receiving from RFM9x
- SX1262 TX uses standard IQ (no inversion) when sending to RFM9x

## Files Modified

1. `/home/nkrueger/dev/data_log/radio/sx1262_driver.py`
   - Disabled DIO2 RF switch control
   - Added RF switch settling delays
   - Improved RF switch state management
   - Fixed preamble length configuration
   - Added preamble_length state tracking

2. `/home/nkrueger/dev/data_log/scripts/radio_debug.py` (new)
   - Comprehensive diagnostic tool for SX1262 issues

## Next Steps

1. **Test the fixes**:
   ```bash
   # Test SX1262 send capability
   python scripts/radio_debug.py --rfm9x-compatible
   ```

2. **Verify cross-radio communication**:
   - Test RFM9x → SX1262
   - Test SX1262 → RFM9x
   - Test SX1262 → SX1262 (with --sx126x-native flag)

3. **If issues persist**, check:
   - GPIO wiring (especially TXEN/RXEN pins)
   - Power supply stability (SX1262 needs clean 3.3V)
   - SPI connection quality
   - Frequency calibration (run calibrate_image for your frequency band)

## References

- SX1262 Datasheet: https://www.semtech.com/products/wireless-rf/lora-transceivers/sx1262
- Waveshare Core1262-868M: https://www.waveshare.com/core1262-868m.htm
- IQ Inversion Application Note: Understanding LoRa IQ polarity for cross-family compatibility
