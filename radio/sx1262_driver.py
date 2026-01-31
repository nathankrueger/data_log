"""
Low-level SX1262 driver using spidev and gpiozero.

This is a minimal driver for the Semtech SX1262 LoRa transceiver that bypasses
the buggy LoRaRF library. It uses spidev for SPI communication (proven to work)
and gpiozero for GPIO control (matching the rest of the project).

Based on the Semtech SX1262 datasheet and application notes.
"""

import time
from typing import Optional

import spidev
from gpiozero import DigitalInputDevice, DigitalOutputDevice


class SX1262Driver:
    """Low-level SX1262 LoRa transceiver driver."""

    # SX1262 Commands (from datasheet)
    CMD_SET_SLEEP = 0x84
    CMD_SET_STANDBY = 0x80
    CMD_SET_FS = 0xC1
    CMD_SET_TX = 0x83
    CMD_SET_RX = 0x82
    CMD_STOP_TIMER_ON_PREAMBLE = 0x9F
    CMD_SET_CAD = 0xC5
    CMD_SET_TX_CONTINUOUS_WAVE = 0xD1
    CMD_SET_TX_INFINITE_PREAMBLE = 0xD2
    CMD_SET_REGULATOR_MODE = 0x96
    CMD_CALIBRATE = 0x89
    CMD_CALIBRATE_IMAGE = 0x98
    CMD_SET_PA_CONFIG = 0x95
    CMD_SET_RX_TX_FALLBACK_MODE = 0x93

    CMD_WRITE_REGISTER = 0x0D
    CMD_READ_REGISTER = 0x1D
    CMD_WRITE_BUFFER = 0x0E
    CMD_READ_BUFFER = 0x1E

    CMD_SET_DIO_IRQ_PARAMS = 0x08
    CMD_GET_IRQ_STATUS = 0x12
    CMD_CLR_IRQ_STATUS = 0x02
    CMD_SET_DIO2_AS_RF_SWITCH_CTRL = 0x9D
    CMD_SET_DIO3_AS_TCXO_CTRL = 0x97

    CMD_SET_RF_FREQUENCY = 0x86
    CMD_SET_PKT_TYPE = 0x8A
    CMD_GET_PKT_TYPE = 0x11
    CMD_SET_TX_PARAMS = 0x8E
    CMD_SET_MODULATION_PARAMS = 0x8B
    CMD_SET_PKT_PARAMS = 0x8C
    CMD_SET_CAD_PARAMS = 0x88
    CMD_SET_BUFFER_BASE_ADDRESS = 0x8F
    CMD_SET_LORA_SYMB_NUM_TIMEOUT = 0xA0

    CMD_GET_STATUS = 0xC0
    CMD_GET_RSSI_INST = 0x15
    CMD_GET_RX_BUFFER_STATUS = 0x13
    CMD_GET_PKT_STATUS = 0x14
    CMD_GET_DEVICE_ERRORS = 0x17
    CMD_CLR_DEVICE_ERRORS = 0x07

    # Packet types
    PKT_TYPE_GFSK = 0x00
    PKT_TYPE_LORA = 0x01

    # Standby modes
    STDBY_RC = 0x00
    STDBY_XOSC = 0x01

    # Regulator modes
    REG_LDO = 0x00
    REG_DC_DC = 0x01

    # LoRa spreading factors
    SF5 = 0x05
    SF6 = 0x06
    SF7 = 0x07
    SF8 = 0x08
    SF9 = 0x09
    SF10 = 0x0A
    SF11 = 0x0B
    SF12 = 0x0C

    # LoRa bandwidths
    BW_7800 = 0x00
    BW_10400 = 0x08
    BW_15600 = 0x01
    BW_20800 = 0x09
    BW_31250 = 0x02
    BW_41700 = 0x0A
    BW_62500 = 0x03
    BW_125000 = 0x04
    BW_250000 = 0x05
    BW_500000 = 0x06

    # LoRa coding rates
    CR_4_5 = 0x01
    CR_4_6 = 0x02
    CR_4_7 = 0x03
    CR_4_8 = 0x04

    # IRQ flags
    IRQ_TX_DONE = 0x0001
    IRQ_RX_DONE = 0x0002
    IRQ_PREAMBLE_DETECTED = 0x0004
    IRQ_SYNC_WORD_VALID = 0x0008
    IRQ_HEADER_VALID = 0x0010
    IRQ_HEADER_ERR = 0x0020
    IRQ_CRC_ERR = 0x0040
    IRQ_CAD_DONE = 0x0080
    IRQ_CAD_DETECTED = 0x0100
    IRQ_TIMEOUT = 0x0200
    IRQ_ALL = 0x03FF

    def __init__(
        self,
        spi_bus: int = 0,
        spi_cs: int = 0,
        reset_pin: int = 22,
        busy_pin: int = 18,
        dio1_pin: int = 16,
        txen_pin: Optional[int] = 6,
        rxen_pin: Optional[int] = 5,
    ):
        """
        Initialize SX1262 driver.

        Args:
            spi_bus: SPI bus number
            spi_cs: SPI chip select
            reset_pin: GPIO pin for RESET
            busy_pin: GPIO pin for BUSY
            dio1_pin: GPIO pin for DIO1 (interrupt)
            txen_pin: GPIO pin for TX enable (RF switch), None if not used
            rxen_pin: GPIO pin for RX enable (RF switch), None if not used
        """
        self._spi_bus = spi_bus
        self._spi_cs = spi_cs

        # Initialize SPI
        self._spi = spidev.SpiDev()
        self._spi.open(spi_bus, spi_cs)
        self._spi.max_speed_hz = 2_000_000  # 2 MHz, conservative
        self._spi.mode = 0

        # Initialize GPIO using gpiozero
        self._reset = DigitalOutputDevice(reset_pin, initial_value=True)
        self._busy = DigitalInputDevice(busy_pin)
        self._dio1 = DigitalInputDevice(dio1_pin)

        # RF switch control (optional)
        self._txen = DigitalOutputDevice(txen_pin, initial_value=False) if txen_pin else None
        self._rxen = DigitalOutputDevice(rxen_pin, initial_value=False) if rxen_pin else None

        # State tracking
        self._frequency = 915_000_000
        self._tx_power = 22
        self._spreading_factor = self.SF7
        self._bandwidth = self.BW_125000
        self._coding_rate = self.CR_4_5

    def reset(self) -> None:
        """Perform hardware reset of the radio."""
        self._reset.off()  # Pull low
        time.sleep(0.001)  # 1ms
        self._reset.on()  # Pull high
        time.sleep(0.005)  # 5ms for startup

    def wait_busy(self, timeout: float = 1.0) -> bool:
        """Wait for BUSY pin to go low."""
        start = time.monotonic()
        while self._busy.is_active:
            if (time.monotonic() - start) > timeout:
                return False
            time.sleep(0.0001)  # 100us
        return True

    def _spi_transfer(self, data: list[int]) -> list[int]:
        """Perform SPI transfer after waiting for BUSY."""
        if not self.wait_busy():
            raise TimeoutError("SX1262 BUSY timeout")
        return self._spi.xfer2(data)

    def _write_command(self, cmd: int, data: list[int] = None) -> None:
        """Write a command with optional data bytes."""
        if data is None:
            data = []
        self._spi_transfer([cmd] + data)

    def _read_command(self, cmd: int, length: int) -> list[int]:
        """Read data from a command."""
        # Most read commands need a NOP byte after the command
        result = self._spi_transfer([cmd, 0x00] + [0x00] * length)
        return result[2:]  # Skip command echo and status byte

    def get_status(self) -> int:
        """Get device status."""
        result = self._spi_transfer([self.CMD_GET_STATUS, 0x00])
        return result[1] if len(result) > 1 else 0

    def set_standby(self, mode: int = None) -> None:
        """Set device to standby mode."""
        if mode is None:
            mode = self.STDBY_RC
        self._write_command(self.CMD_SET_STANDBY, [mode])

    def set_packet_type(self, pkt_type: int) -> None:
        """Set packet type (LoRa or FSK)."""
        self._write_command(self.CMD_SET_PKT_TYPE, [pkt_type])

    def set_frequency(self, freq_hz: int) -> None:
        """Set RF frequency in Hz."""
        self._frequency = freq_hz
        # Frequency calculation: freq_reg = freq_hz * 2^25 / 32_000_000
        freq_reg = int(freq_hz * (1 << 25) / 32_000_000)
        self._write_command(self.CMD_SET_RF_FREQUENCY, [
            (freq_reg >> 24) & 0xFF,
            (freq_reg >> 16) & 0xFF,
            (freq_reg >> 8) & 0xFF,
            freq_reg & 0xFF,
        ])

    def set_pa_config(self, pa_duty_cycle: int = 0x04, hp_max: int = 0x07,
                      device_sel: int = 0x00, pa_lut: int = 0x01) -> None:
        """Configure power amplifier."""
        self._write_command(self.CMD_SET_PA_CONFIG, [pa_duty_cycle, hp_max, device_sel, pa_lut])

    def set_tx_params(self, power: int = 22, ramp_time: int = 0x04) -> None:
        """Set TX power and ramp time."""
        self._tx_power = power
        # Power is in dBm, range -9 to +22 for SX1262
        power_byte = power & 0xFF  # Two's complement for negative
        self._write_command(self.CMD_SET_TX_PARAMS, [power_byte, ramp_time])

    def set_modulation_params(self, sf: int = None, bw: int = None, cr: int = None,
                               low_data_rate_opt: int = 0) -> None:
        """Set LoRa modulation parameters."""
        if sf is not None:
            self._spreading_factor = sf
        if bw is not None:
            self._bandwidth = bw
        if cr is not None:
            self._coding_rate = cr

        self._write_command(self.CMD_SET_MODULATION_PARAMS, [
            self._spreading_factor,
            self._bandwidth,
            self._coding_rate,
            low_data_rate_opt,
        ])

    def set_packet_params(self, preamble_len: int = 8, header_type: int = 0,
                          payload_len: int = 255, crc_on: int = 1, invert_iq: int = 0) -> None:
        """Set LoRa packet parameters."""
        self._write_command(self.CMD_SET_PKT_PARAMS, [
            (preamble_len >> 8) & 0xFF,
            preamble_len & 0xFF,
            header_type,  # 0 = explicit, 1 = implicit
            payload_len,
            crc_on,
            invert_iq,
        ])

    def set_buffer_base_address(self, tx_base: int = 0x00, rx_base: int = 0x00) -> None:
        """Set TX and RX buffer base addresses."""
        self._write_command(self.CMD_SET_BUFFER_BASE_ADDRESS, [tx_base, rx_base])

    def set_dio_irq_params(self, irq_mask: int = IRQ_ALL, dio1_mask: int = IRQ_ALL,
                           dio2_mask: int = 0, dio3_mask: int = 0) -> None:
        """Configure IRQ and DIO mapping."""
        self._write_command(self.CMD_SET_DIO_IRQ_PARAMS, [
            (irq_mask >> 8) & 0xFF, irq_mask & 0xFF,
            (dio1_mask >> 8) & 0xFF, dio1_mask & 0xFF,
            (dio2_mask >> 8) & 0xFF, dio2_mask & 0xFF,
            (dio3_mask >> 8) & 0xFF, dio3_mask & 0xFF,
        ])

    def clear_irq_status(self, irq_mask: int = IRQ_ALL) -> None:
        """Clear IRQ flags."""
        self._write_command(self.CMD_CLR_IRQ_STATUS, [
            (irq_mask >> 8) & 0xFF, irq_mask & 0xFF
        ])

    def get_irq_status(self) -> int:
        """Get current IRQ status."""
        result = self._read_command(self.CMD_GET_IRQ_STATUS, 2)
        if len(result) >= 2:
            return (result[0] << 8) | result[1]
        return 0

    def set_dio2_as_rf_switch(self, enable: bool = True) -> None:
        """Use DIO2 to control RF switch."""
        self._write_command(self.CMD_SET_DIO2_AS_RF_SWITCH_CTRL, [0x01 if enable else 0x00])

    def set_regulator_mode(self, mode: int = None) -> None:
        """Set regulator mode (LDO or DC-DC)."""
        if mode is None:
            mode = self.REG_DC_DC
        self._write_command(self.CMD_SET_REGULATOR_MODE, [mode])

    def calibrate(self, calib_param: int = 0x7F) -> None:
        """Run calibration."""
        self._write_command(self.CMD_CALIBRATE, [calib_param])
        time.sleep(0.01)  # Wait for calibration

    def set_sync_word(self, sync_word: int) -> None:
        """Set LoRa sync word (2 bytes for SX126x)."""
        # Write to registers 0x0740 and 0x0741
        self._write_command(self.CMD_WRITE_REGISTER, [
            0x07, 0x40,  # Address high, low
            (sync_word >> 8) & 0xFF,
            sync_word & 0xFF,
        ])

    def write_buffer(self, offset: int, data: bytes) -> None:
        """Write data to TX buffer."""
        self._write_command(self.CMD_WRITE_BUFFER, [offset] + list(data))

    def read_buffer(self, offset: int, length: int) -> bytes:
        """Read data from RX buffer."""
        result = self._spi_transfer([self.CMD_READ_BUFFER, offset, 0x00] + [0x00] * length)
        return bytes(result[3:])  # Skip command, offset, status

    def get_rx_buffer_status(self) -> tuple[int, int]:
        """Get RX buffer status (payload length, buffer start)."""
        result = self._read_command(self.CMD_GET_RX_BUFFER_STATUS, 2)
        if len(result) >= 2:
            return result[0], result[1]
        return 0, 0

    def get_packet_status(self) -> tuple[int, int, int]:
        """Get packet status (RSSI, SNR, signal RSSI)."""
        result = self._read_command(self.CMD_GET_PKT_STATUS, 3)
        if len(result) >= 3:
            rssi = -result[0] // 2
            snr = result[1] // 4 if result[1] < 128 else (result[1] - 256) // 4
            signal_rssi = -result[2] // 2
            return rssi, snr, signal_rssi
        return 0, 0, 0

    def set_rf_switch_rx(self) -> None:
        """Set RF switch to RX mode."""
        if self._txen:
            self._txen.off()
        if self._rxen:
            self._rxen.on()

    def set_rf_switch_tx(self) -> None:
        """Set RF switch to TX mode."""
        if self._rxen:
            self._rxen.off()
        if self._txen:
            self._txen.on()

    def set_rf_switch_off(self) -> None:
        """Turn off RF switch."""
        if self._txen:
            self._txen.off()
        if self._rxen:
            self._rxen.off()

    def set_tx(self, timeout_ms: int = 0) -> None:
        """Start TX mode."""
        self.set_rf_switch_tx()
        # Timeout in 15.625us steps, 0 = no timeout
        timeout = int(timeout_ms * 1000 / 15.625) if timeout_ms > 0 else 0
        self._write_command(self.CMD_SET_TX, [
            (timeout >> 16) & 0xFF,
            (timeout >> 8) & 0xFF,
            timeout & 0xFF,
        ])

    def set_rx(self, timeout_ms: int = 0) -> None:
        """Start RX mode."""
        self.set_rf_switch_rx()
        # Timeout in 15.625us steps, 0xFFFFFF = continuous
        if timeout_ms == 0:
            timeout = 0xFFFFFF
        else:
            timeout = int(timeout_ms * 1000 / 15.625)
        self._write_command(self.CMD_SET_RX, [
            (timeout >> 16) & 0xFF,
            (timeout >> 8) & 0xFF,
            timeout & 0xFF,
        ])

    def begin(self) -> bool:
        """Initialize the radio. Returns True on success."""
        try:
            # Hardware reset
            self.reset()

            # Check we can communicate
            status = self.get_status()
            if status == 0:
                return False

            # Set to standby
            self.set_standby(self.STDBY_RC)
            time.sleep(0.001)

            # Configure for LoRa
            self.set_packet_type(self.PKT_TYPE_LORA)
            self.set_regulator_mode(self.REG_DC_DC)
            self.calibrate()

            # Set buffer base addresses
            self.set_buffer_base_address(0x00, 0x00)

            # Configure DIO and IRQ
            self.set_dio_irq_params(self.IRQ_ALL, self.IRQ_ALL, 0, 0)
            self.clear_irq_status()

            # Enable DIO2 for RF switch if using it
            self.set_dio2_as_rf_switch(True)

            return True

        except Exception:
            return False

    def configure(
        self,
        frequency_hz: int = 915_000_000,
        tx_power: int = 22,
        spreading_factor: int = None,
        bandwidth: int = None,
        coding_rate: int = None,
        sync_word: int = 0x1424,
        preamble_len: int = 8,
    ) -> None:
        """Configure radio parameters."""
        if spreading_factor is None:
            spreading_factor = self.SF7
        if bandwidth is None:
            bandwidth = self.BW_125000
        if coding_rate is None:
            coding_rate = self.CR_4_5

        self.set_standby()
        self.set_frequency(frequency_hz)

        # PA config for +22dBm
        self.set_pa_config(0x04, 0x07, 0x00, 0x01)
        self.set_tx_params(tx_power, 0x04)

        self.set_modulation_params(spreading_factor, bandwidth, coding_rate)
        self.set_packet_params(preamble_len, 0, 255, 1, 0)
        self.set_sync_word(sync_word)

    def send(self, data: bytes, timeout_ms: int = 5000) -> bool:
        """Send data. Returns True on success."""
        try:
            self.set_standby()
            self.clear_irq_status()

            # Write data to buffer
            self.set_buffer_base_address(0x00, 0x00)
            self.write_buffer(0x00, data)

            # Update packet length
            self.set_packet_params(8, 0, len(data), 1, 0)

            # Start TX
            self.set_tx(timeout_ms)

            # Wait for TX done or timeout
            start = time.monotonic()
            while (time.monotonic() - start) < (timeout_ms / 1000.0 + 0.5):
                irq = self.get_irq_status()
                if irq & self.IRQ_TX_DONE:
                    self.clear_irq_status()
                    self.set_rf_switch_off()
                    return True
                if irq & self.IRQ_TIMEOUT:
                    self.clear_irq_status()
                    self.set_rf_switch_off()
                    return False
                time.sleep(0.001)

            self.set_rf_switch_off()
            return False

        except Exception:
            self.set_rf_switch_off()
            return False

    def receive(self, timeout_ms: int = 5000) -> Optional[tuple[bytes, int, int]]:
        """
        Receive data with timeout.

        Returns:
            Tuple of (data, rssi, snr) on success, None on timeout/error
        """
        try:
            self.set_standby()
            self.clear_irq_status()
            self.set_buffer_base_address(0x00, 0x00)

            # Start RX
            if timeout_ms == 0:
                self.set_rx(0)  # Continuous
            else:
                self.set_rx(timeout_ms)

            # Wait for RX done or timeout
            start = time.monotonic()
            timeout_sec = timeout_ms / 1000.0 if timeout_ms > 0 else float('inf')

            while (time.monotonic() - start) < (timeout_sec + 0.5):
                irq = self.get_irq_status()

                if irq & self.IRQ_RX_DONE:
                    self.clear_irq_status()
                    self.set_rf_switch_off()

                    # Check for CRC error
                    if irq & self.IRQ_CRC_ERR:
                        return None

                    # Get packet info
                    length, start_ptr = self.get_rx_buffer_status()
                    rssi, snr, _ = self.get_packet_status()

                    # Read data
                    data = self.read_buffer(start_ptr, length)
                    return data, rssi, snr

                if irq & self.IRQ_TIMEOUT:
                    self.clear_irq_status()
                    self.set_rf_switch_off()
                    return None

                time.sleep(0.001)

            self.set_rf_switch_off()
            return None

        except Exception:
            self.set_rf_switch_off()
            return None

    def close(self) -> None:
        """Clean up resources."""
        try:
            self.set_standby()
            self.set_rf_switch_off()
        except Exception:
            pass

        try:
            self._spi.close()
        except Exception:
            pass

        for gpio in [self._reset, self._busy, self._dio1, self._txen, self._rxen]:
            if gpio:
                try:
                    gpio.close()
                except Exception:
                    pass
