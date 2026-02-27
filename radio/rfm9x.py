"""RFM9x LoRa radio implementation."""

import logging
import os
import time

from .base import Radio

logger = logging.getLogger(__name__)


class RFM9xRadio(Radio):
    """
    Adafruit RFM9x LoRa radio implementation.

    Supports two backends:
      - "rpi": Native SPI/GPIO on Raspberry Pi (default)
      - "ft232h": FT232H USB-to-SPI adapter (macOS, Pi, WSL, Linux)

    Wiring (RPi native SPI):
        VIN  -> 3.3V
        GND  -> GND
        SCK  -> GPIO 11 (SPI0 SCLK)
        MISO -> GPIO 9  (SPI0 MISO)
        MOSI -> GPIO 10 (SPI0 MOSI)
        CS   -> Configurable GPIO (default: GPIO 24)
        RST  -> Configurable GPIO (default: GPIO 25)

    Wiring (FT232H):
        VIN  -> 3.3V
        GND  -> GND
        SCK  -> AD0 (SCLK)
        MOSI -> AD1 (DO)
        MISO -> AD2 (DI)
        CS   -> AD4 (GPIO) — default "D4"
        RST  -> AD5 (GPIO) — default "D5"
    """

    def __init__(
        self,
        frequency_mhz: float = 915.0,
        tx_power: int = 23,
        spreading_factor: int = 7,
        signal_bandwidth: int = 125000,
        cs_pin: int | str = 24,
        reset_pin: int | str = 25,
        backend: str = "rpi",
    ):
        """
        Initialize RFM9x radio configuration.

        Args:
            frequency_mhz: Radio frequency (915.0 for US, 868.0 for EU)
            tx_power: Transmit power in dBm (5-23)
            spreading_factor: LoRa spreading factor (7-12)
            signal_bandwidth: Signal bandwidth in Hz (125000, 250000, 500000)
            cs_pin: Chip select pin — int GPIO number for RPi (e.g. 24),
                    or str board pin name for FT232H (e.g. "D4")
            reset_pin: Reset pin — int GPIO number for RPi (e.g. 25),
                       or str board pin name for FT232H (e.g. "D5")
            backend: "rpi" for native SPI/GPIO, "ft232h" for USB-to-SPI adapter
        """
        if backend not in ("rpi", "ft232h"):
            raise ValueError(f"Unknown backend: {backend!r}. Must be 'rpi' or 'ft232h'.")

        self._frequency_mhz = frequency_mhz
        self._tx_power = tx_power
        self._cs_pin = cs_pin
        self._reset_pin = reset_pin
        self._spreading_factor = spreading_factor
        self._signal_bandwidth = signal_bandwidth
        self._backend = backend

        self._rfm9x = None
        self._spi = None
        self._cs = None
        self._reset = None
        self._in_rx = False  # Track RX_CONTINUOUS state (FT232H optimization)

        # FT232H polling stats (logged every 30s)
        self._rx_polls = 0
        self._rx_listens = 0
        self._rx_skipped_listens = 0
        self._rx_stats_time = 0.0

    @staticmethod
    def _resolve_pin(board_module, pin: int | str):
        """Resolve a pin specifier to a board pin object.

        Args:
            board_module: The imported board module.
            pin: int GPIO number (e.g. 24 -> board.D24) or
                 str pin name (e.g. "D4" -> board.D4).
        """
        if isinstance(pin, int):
            attr_name = f"D{pin}"
        elif isinstance(pin, str):
            attr_name = pin
        else:
            raise ValueError(f"Pin must be int or str, got {type(pin).__name__}: {pin}")
        try:
            return getattr(board_module, attr_name)
        except AttributeError:
            raise ValueError(f"Pin '{attr_name}' not found on board module")

    def init(self) -> None:
        """Initialize the RFM9x radio hardware."""
        if self._backend == "ft232h":
            os.environ["BLINKA_FT232H"] = "1"

        import board
        import busio
        import digitalio
        import adafruit_rfm9x

        cs_board_pin = self._resolve_pin(board, self._cs_pin)
        reset_board_pin = self._resolve_pin(board, self._reset_pin)

        self._spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)
        self._cs = digitalio.DigitalInOut(cs_board_pin)
        self._reset = digitalio.DigitalInOut(reset_board_pin)

        self._rfm9x = adafruit_rfm9x.RFM9x(
            self._spi, self._cs, self._reset, self._frequency_mhz
        )
        self._rfm9x.tx_power = self._tx_power
        
        # Match AB01 Arduino radio settings
        self._rfm9x.spreading_factor = self._spreading_factor
        self._rfm9x.signal_bandwidth = self._signal_bandwidth
        self._rfm9x.coding_rate = 5           # 4/5 (library uses denominator)
        self._rfm9x.preamble_length = 8       # 8 symbol preamble
        self._rfm9x.enable_crc = True         # Enable CRC (should be default, but explicit)
        self._rx_stats_time = time.monotonic()

    def send(self, data: bytes) -> bool:
        """Send data over LoRa."""
        if self._rfm9x is None:
            raise RuntimeError("Radio not initialized. Call init() first.")
        try:
            self._rfm9x.send(data)
            self._in_rx = False
            return True
        except Exception as e:
            logger.warning(f"Radio send failed: {e} (payload size: {len(data)} bytes)")
            return False

    def receive(self, timeout: float = 5.0) -> bytes | None:
        """Receive data from LoRa with timeout."""
        if self._rfm9x is None:
            raise RuntimeError("Radio not initialized. Call init() first.")
        if self._backend == "ft232h":
            # Efficient polling: listen mode + periodic rx_done check with sleeps.
            # The Adafruit library's receive() busy-polls the IRQ register with no
            # sleep. Over native RPi SPI this is cheap (memory-mapped), but over
            # FT232H USB-SPI each poll is a ~1ms USB roundtrip that burns CPU.
            #
            # We also track RX state to skip redundant listen() calls.  The radio
            # stays in RX_CONTINUOUS until something changes mode (send, idle,
            # hard_reset, or the library's receive(timeout=0) which calls idle()).
            # Each listen() is 2-3 SPI writes over USB (~4-6ms), so skipping it
            # on every poll cycle saves ~30 USB transactions/sec.
            if not self._in_rx:
                self._rfm9x.listen()
                self._in_rx = True
                self._rx_listens += 1
            else:
                self._rx_skipped_listens += 1
            deadline = time.monotonic() + timeout
            while True:
                self._rx_polls += 1
                if self._rfm9x.rx_done():
                    packet = self._rfm9x.receive(timeout=0)
                    # receive(timeout=0) calls idle() internally, leaving the
                    # radio in standby.  Re-enter RX immediately.
                    self._rfm9x.listen()
                    self._rx_listens += 1
                    # _in_rx stays True
                    return packet
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._log_rx_stats()
                    return None
                time.sleep(min(0.1, remaining))
        else:
            return self._rfm9x.receive(timeout=timeout)

    def _log_rx_stats(self) -> None:
        """Log FT232H polling stats every 30s."""
        now = time.monotonic()
        elapsed = now - self._rx_stats_time
        if elapsed < 30:
            return
        polls_sec = self._rx_polls / elapsed if elapsed > 0 else 0
        logger.info(
            "FT232H RX stats (%.0fs): polls=%d (%.1f/s), "
            "listen_calls=%d, listen_skipped=%d",
            elapsed, self._rx_polls, polls_sec,
            self._rx_listens, self._rx_skipped_listens,
        )
        self._rx_polls = 0
        self._rx_listens = 0
        self._rx_skipped_listens = 0
        self._rx_stats_time = now

    def listen(self) -> None:
        """Enter receive mode (like AB01's Radio.Rx(0)).

        Call this once, then poll rx_done() to check for packets.
        More efficient than repeated receive() calls for long RX windows.
        """
        if self._rfm9x is None:
            raise RuntimeError("Radio not initialized. Call init() first.")
        self._rfm9x.listen()

    def rx_done(self) -> bool:
        """Check if a packet has been received.

        Use with listen() for efficient polling:
            radio.listen()
            while not radio.rx_done():
                time.sleep(0.1)
            packet = radio.receive(timeout=0)
        """
        if self._rfm9x is None:
            return False
        return self._rfm9x.rx_done()

    def get_last_rssi(self) -> int | None:
        """Get RSSI of last received packet."""
        if self._rfm9x is None:
            return None
        return self._rfm9x.last_rssi

    def close(self) -> None:
        """Clean up radio resources."""
        # The adafruit library doesn't have explicit cleanup,
        # but we clear our references
        self._rfm9x = None
        self._in_rx = False
        if self._spi:
            self._spi.deinit()
            self._spi = None
        self._cs = None
        self._reset = None

    def idle(self) -> None:
        """Enter standby mode (stops RX/TX)."""
        if self._rfm9x is None:
            raise RuntimeError("Radio not initialized. Call init() first.")
        self._rfm9x.idle()
        self._in_rx = False

    def recover_rx(self) -> None:
        """Soft-recover from a stuck rx_done state.

        Cycles SLEEP→STANDBY to reset the LoRa state machine, resets the FIFO
        pointer to the RX base address, and clears all IRQ flags.  SLEEP
        preserves register config (SF, BW, frequency, etc.) but FIFO pointer
        registers survive the cycle and must be reset explicitly.
        """
        if self._rfm9x is None:
            raise RuntimeError("Radio not initialized. Call init() first.")
        self._rfm9x.sleep()
        time.sleep(0.01)
        self._rfm9x.idle()
        self._in_rx = False
        # Reset FIFO pointer to RX base address (stale pointers survive SLEEP)
        rx_base = self._rfm9x._read_u8(0x0F)   # RegFifoRxBaseAddr
        self._rfm9x._write_u8(0x0D, rx_base)    # RegFifoAddrPtr
        self._rfm9x._write_u8(0x12, 0xFF)       # Clear all IRQ flags

    def hard_reset(self) -> None:
        """Hardware reset via pin toggle and full register re-initialization.

        Wipes all registers.  Restores LoRa mode and all RF parameters from
        cached values (NOT live registers, which may be corrupted).
        """
        if self._rfm9x is None:
            raise RuntimeError("Radio not initialized. Call init() first.")

        logger.warning("Hard reset (SF=%d, BW=%d, freq=%.1f, txpwr=%d)",
                       self._spreading_factor, self._signal_bandwidth,
                       self._frequency_mhz, self._tx_power)

        # Toggle hardware reset pin (LOW 100μs → HIGH 5ms)
        self._rfm9x.reset()

        # Re-enter LoRa mode (must be set in SLEEP)
        self._rfm9x.sleep()
        time.sleep(0.01)
        self._rfm9x.long_range_mode = True

        # FIFO base addresses (cleared by hardware reset)
        self._rfm9x._write_u8(0x0E, 0x00)  # RegFifoTxBaseAddr
        self._rfm9x._write_u8(0x0F, 0x00)  # RegFifoRxBaseAddr

        # Restore all RF parameters from cached values
        self._rfm9x.idle()
        self._rfm9x.frequency_mhz = self._frequency_mhz
        self._rfm9x.tx_power = self._tx_power
        self._rfm9x.spreading_factor = self._spreading_factor
        self._rfm9x.signal_bandwidth = self._signal_bandwidth
        self._rfm9x.coding_rate = 5
        self._rfm9x.preamble_length = 8
        self._rfm9x.enable_crc = True
        self._rfm9x._write_u8(0x12, 0xFF)  # Clear all IRQ flags
        self._in_rx = False

    def set_frequency(self, frequency_mhz: float) -> None:
        """Change the radio frequency at runtime.

        Does NOT enter STANDBY first — callers may be in RX_CONTINUOUS.
        """
        if self._rfm9x is None:
            raise RuntimeError("Radio not initialized. Call init() first.")
        self._rfm9x.frequency_mhz = frequency_mhz
        self._frequency_mhz = frequency_mhz

    @property
    def frequency_mhz(self) -> float:
        """Get the configured frequency."""
        return self._frequency_mhz

    @property
    def tx_power(self) -> int:
        """Get the configured transmit power."""
        return self._tx_power

    @tx_power.setter
    def tx_power(self, value: int) -> None:
        """Set the transmit power (5-23 dBm)."""
        self._tx_power = value
        if self._rfm9x is not None:
            self._rfm9x.tx_power = value

    @property
    def spreading_factor(self) -> int:
        """Get the current spreading factor (cached)."""
        return self._spreading_factor

    @spreading_factor.setter
    def spreading_factor(self, value: int) -> None:
        """Set the spreading factor (7-12)."""
        self._spreading_factor = value
        if self._rfm9x is not None:
            self._rfm9x.spreading_factor = value

    @property
    def signal_bandwidth(self) -> int:
        """Get the current signal bandwidth in Hz (cached)."""
        return self._signal_bandwidth

    @signal_bandwidth.setter
    def signal_bandwidth(self, value: int) -> None:
        """Set the signal bandwidth in Hz (125000, 250000, or 500000)."""
        self._signal_bandwidth = value
        if self._rfm9x is not None:
            self._rfm9x.signal_bandwidth = value


# RSSI to brightness mapping utilities
RSSI_MAX = -50   # Strong signal
RSSI_MIN = -120  # Weak signal (RFM9x sensitivity limit)


def rssi_to_brightness(rssi: float, led_min: int = 0, led_max: int = 60) -> int:
    """
    Convert RSSI (dBm) to LED brightness.

    Args:
        rssi: Signal strength in dBm
        led_min: Minimum brightness value
        led_max: Maximum brightness value

    Returns:
        Brightness value between led_min and led_max
    """
    rssi = max(RSSI_MIN, min(RSSI_MAX, rssi))
    brightness = int(
        (rssi - RSSI_MIN) / (RSSI_MAX - RSSI_MIN) * (led_max - led_min) + led_min
    )
    return brightness
