"""Waveshare Core1262-868M LoRa radio implementation using LoRaRF library."""

import logging
import time

from .base import Radio

logger = logging.getLogger(__name__)


class SX1262Radio(Radio):
    """
    Waveshare Core1262-868M LoRa radio module implementation.

    Uses the LoRaRF library: pip install LoRaRF
    See: https://github.com/chandrawi/LoRaRF-Python

    Configured to be interoperable with RFM9x radios using matching
    LoRa modulation parameters (spreading factor, bandwidth, coding rate).

    This module has an RF switch requiring RXEN/TXEN control:
        - RXEN=LOW,  TXEN=HIGH -> Receive mode
        - RXEN=HIGH, TXEN=LOW  -> Transmit mode

    Wiring (Core1262 to Raspberry Pi):
        See pinout: _assets/ws_sx1262_pinout.png

        3V3   -> 3.3V
        GND   -> GND
        CLK   -> GPIO 11 (SPI0 SCLK)
        MISO  -> GPIO 9  (SPI0 MISO)
        MOSI  -> GPIO 10 (SPI0 MOSI)
        CS    -> GPIO 8  (SPI0 CE0) or configurable
        BUSY  -> Configurable GPIO (default: GPIO 18)
        RESET -> Configurable GPIO (default: GPIO 22)
        DIO1  -> Configurable GPIO (default: GPIO 16)
        TXEN  -> Configurable GPIO (default: GPIO 6)
        RXEN  -> Configurable GPIO (default: GPIO 5)
    """

    # Default LoRa parameters for RFM9x interoperability
    # RFM9x defaults: SF=7, BW=125kHz, CR=4/5
    DEFAULT_SPREADING_FACTOR = 7
    DEFAULT_BANDWIDTH = 125000  # 125 kHz
    DEFAULT_CODING_RATE = 5  # 4/5

    def __init__(
        self,
        frequency_mhz: float = 915.0,
        tx_power: int = 22,
        spreading_factor: int = DEFAULT_SPREADING_FACTOR,
        bandwidth: int = DEFAULT_BANDWIDTH,
        coding_rate: int = DEFAULT_CODING_RATE,
        preamble_length: int = 8,
        busy_pin: int = 18,
        reset_pin: int = 22,
        dio1_pin: int = 16,
        txen_pin: int = 6,
        rxen_pin: int = 5,
        spi_bus: int = 0,
        spi_cs: int = 0,
    ):
        """
        Initialize SX1262 radio configuration.

        Args:
            frequency_mhz: Radio frequency (915.0 for US, 868.0 for EU)
            tx_power: Transmit power in dBm (max 22 for SX1262)
            spreading_factor: LoRa spreading factor (7-12), must match RFM9x
            bandwidth: Bandwidth in Hz (125000, 250000, or 500000)
            coding_rate: Coding rate denominator (5-8 for 4/5 to 4/8)
            preamble_length: Preamble length in symbols
            busy_pin: GPIO pin for BUSY signal
            reset_pin: GPIO pin for RST signal
            dio1_pin: GPIO pin for DIO1 interrupt
            txen_pin: GPIO pin for TX enable (RF switch)
            rxen_pin: GPIO pin for RX enable (RF switch)
            spi_bus: SPI bus number
            spi_cs: SPI chip select
        """
        self._frequency_mhz = frequency_mhz
        self._tx_power = min(tx_power, 22)  # SX1262 max is 22 dBm
        self._spreading_factor = spreading_factor
        self._bandwidth = bandwidth
        self._coding_rate = coding_rate
        self._preamble_length = preamble_length

        self._busy_pin = busy_pin
        self._reset_pin = reset_pin
        self._dio1_pin = dio1_pin
        self._txen_pin = txen_pin
        self._rxen_pin = rxen_pin
        self._spi_bus = spi_bus
        self._spi_cs = spi_cs

        self._lora = None
        self._last_rssi: int | None = None

    def _hardware_reset(self) -> None:
        """Perform a hardware reset of the radio via the RESET pin.

        This clears any stuck state from a previous run that didn't clean up.
        """
        try:
            import lgpio

            h = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_output(h, self._reset_pin)

            # Pulse reset low for 1ms, then high
            lgpio.gpio_write(h, self._reset_pin, 0)  # Low
            time.sleep(0.001)
            lgpio.gpio_write(h, self._reset_pin, 1)  # High
            time.sleep(0.01)  # Wait for radio to stabilize

            lgpio.gpio_free(h, self._reset_pin)
            lgpio.gpiochip_close(h)
            logger.debug("Hardware reset complete")
        except Exception as e:
            # If we can't do a hardware reset, continue anyway
            # The begin() call will do its own reset
            logger.debug(f"Hardware reset skipped: {e}")

    def init(self) -> None:
        """Initialize the SX1262 radio hardware."""
        try:
            from LoRaRF import SX126x
        except ImportError:
            raise ImportError(
                "LoRaRF library not found. Install with: pip install LoRaRF"
            )

        self._lora = SX126x()

        logger.debug(
            f"Configuring SX1262: SPI bus={self._spi_bus}, cs={self._spi_cs}, "
            f"reset={self._reset_pin}, busy={self._busy_pin}, dio1={self._dio1_pin}, "
            f"txen={self._txen_pin}, rxen={self._rxen_pin}"
        )

        # Configure SPI - must be called before begin()
        self._lora.setSpi(self._spi_bus, self._spi_cs)

        # Configure GPIO pins: reset, busy, dio1, txen, rxen
        # These must be set before begin()
        self._lora.setPins(
            self._reset_pin,
            self._busy_pin,
            self._dio1_pin,
            self._txen_pin,
            self._rxen_pin,
        )

        # Initialize the radio
        logger.debug("Calling begin()...")
        if not self._lora.begin():
            raise RuntimeError(
                f"Failed to initialize SX1262 radio. Check:\n"
                f"  1. SPI enabled: ls /dev/spi*\n"
                f"  2. Wiring: RESET={self._reset_pin}, BUSY={self._busy_pin}, "
                f"DIO1={self._dio1_pin}, TXEN={self._txen_pin}, RXEN={self._rxen_pin}\n"
                f"  3. SPI: bus={self._spi_bus}, cs={self._spi_cs} (CS pin GPIO 8 for CE0, GPIO 7 for CE1)"
            )

        # Set frequency in Hz
        freq_hz = int(self._frequency_mhz * 1_000_000)
        self._lora.setFrequency(freq_hz)

        # Bandwidth mapping for LoRaRF library (uses bandwidth index)
        # BW_7800 = 0, BW_10400 = 1, BW_15600 = 2, BW_20800 = 3,
        # BW_31250 = 4, BW_41700 = 5, BW_62500 = 6, BW_125000 = 7,
        # BW_250000 = 8, BW_500000 = 9
        bw_map = {
            125000: 7,
            250000: 8,
            500000: 9,
        }
        bw_index = bw_map.get(self._bandwidth, 7)

        # Coding rate: CR_4_5 = 1, CR_4_6 = 2, CR_4_7 = 3, CR_4_8 = 4
        cr_index = self._coding_rate - 4

        # Set LoRa modulation parameters for RFM9x interoperability
        self._lora.setLoRaModulation(self._spreading_factor, bw_index, cr_index)

        # Set packet parameters
        # headerType: HEADER_EXPLICIT = 0, HEADER_IMPLICIT = 1
        # crcType: CRC_DISABLE = 0, CRC_ENABLE = 1
        self._lora.setLoRaPacket(
            self._preamble_length,
            0,    # Explicit header (variable length) for RFM9x compatibility
            255,  # Max payload length
            1,    # CRC enabled
            0,    # Standard IQ (not inverted)
        )

        # Set sync word for LoRa compatibility with RFM9x (SX127x)
        # SX127x uses single-byte sync word, SX126x uses two-byte format
        # Conversion: SX127x 0x12 -> SX126x 0x1424, SX127x 0x34 -> SX126x 0x3444
        # The formula is: ((sw & 0xF0) << 8) | 0x04 | ((sw & 0x0F) << 4) | 0x04
        # For 0x12: 0x1424
        self._lora.setSyncWord(0x1424)

        # Set TX power
        self._lora.setTxPower(self._tx_power)

        logger.info(
            f"SX1262 initialized: {self._frequency_mhz} MHz, "
            f"SF{self._spreading_factor}, BW {self._bandwidth}Hz, "
            f"CR 4/{self._coding_rate}, TX {self._tx_power} dBm, "
            f"sync=0x1424 (RFM9x-compatible)"
        )

    def send(self, data: bytes) -> bool:
        """Send data over LoRa."""
        if self._lora is None:
            raise RuntimeError("Radio not initialized. Call init() first.")

        try:
            # Transmit the message and wait for completion
            self._lora.beginPacket()
            self._lora.write(list(data), len(data))
            self._lora.endPacket()
            self._lora.wait()
            return True
        except Exception as e:
            logger.warning(f"Radio send failed: {e} (payload size: {len(data)} bytes)")
            return False

    def receive(self, timeout: float = 5.0) -> bytes | None:
        """Receive data from LoRa with timeout."""
        if self._lora is None:
            raise RuntimeError("Radio not initialized. Call init() first.")

        timeout_ms = int(timeout * 1000)

        try:
            # Set to receive mode with timeout
            self._lora.request(timeout_ms)

            # Wait for packet or timeout
            self._lora.wait()

            # Check if we received data
            length = self._lora.available()
            if length > 0:
                # Read the packet
                data = []
                while self._lora.available():
                    data.append(self._lora.read())

                # Store signal quality metrics
                self._last_rssi = int(self._lora.packetRssi())
                return bytes(data)

            return None

        except Exception as e:
            logger.warning(f"Radio receive failed: {e}")
            return None

    def get_last_rssi(self) -> int | None:
        """Get RSSI of last received packet."""
        return self._last_rssi

    def close(self) -> None:
        """Clean up radio resources."""
        if self._lora is not None:
            try:
                self._lora.sleep()
            except Exception:
                pass
            self._lora = None
        self._last_rssi = None

    @property
    def frequency_mhz(self) -> float:
        """Get the configured frequency."""
        return self._frequency_mhz

    @property
    def tx_power(self) -> int:
        """Get the configured transmit power."""
        return self._tx_power

    @property
    def spreading_factor(self) -> int:
        """Get the configured spreading factor."""
        return self._spreading_factor

    @property
    def bandwidth(self) -> int:
        """Get the configured bandwidth in Hz."""
        return self._bandwidth

    def get_last_snr(self) -> float | None:
        """
        Get SNR (Signal-to-Noise Ratio) of last received packet.

        Returns:
            SNR in dB, or None if not available
        """
        if self._lora is None:
            return None
        try:
            return self._lora.snr()
        except Exception:
            return None
