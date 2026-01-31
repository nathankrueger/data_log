"""Waveshare Core1262-868M LoRa radio implementation using custom spidev driver."""

import logging

from .base import Radio
from .sx1262_driver import SX1262Driver

logger = logging.getLogger(__name__)


class SX1262Radio(Radio):
    """
    Waveshare Core1262-868M LoRa radio module implementation.

    Uses a custom spidev + gpiozero driver that bypasses the buggy LoRaRF library.
    Configured to be interoperable with RFM9x radios using matching
    LoRa modulation parameters (spreading factor, bandwidth, coding rate).

    This module has an RF switch requiring RXEN/TXEN control:
        - RXEN=LOW,  TXEN=HIGH -> Transmit mode
        - RXEN=HIGH, TXEN=LOW  -> Receive mode

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
        rfm9x_compatible: bool = True,
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
            rfm9x_compatible: If True, use IQ inversion for interoperability
                with SX127x/RFM9x radios. Set False for SX1262-only networks.
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
        self._rfm9x_compatible = rfm9x_compatible

        self._driver: SX1262Driver | None = None
        self._last_rssi: int | None = None
        self._last_snr: float | None = None

    def init(self) -> None:
        """Initialize the SX1262 radio hardware."""
        logger.debug(
            f"Configuring SX1262: SPI bus={self._spi_bus}, cs={self._spi_cs}, "
            f"reset={self._reset_pin}, busy={self._busy_pin}, dio1={self._dio1_pin}, "
            f"txen={self._txen_pin}, rxen={self._rxen_pin}"
        )

        # Create driver instance
        self._driver = SX1262Driver(
            spi_bus=self._spi_bus,
            spi_cs=self._spi_cs,
            reset_pin=self._reset_pin,
            busy_pin=self._busy_pin,
            dio1_pin=self._dio1_pin,
            txen_pin=self._txen_pin,
            rxen_pin=self._rxen_pin,
            rfm9x_compatible=self._rfm9x_compatible,
        )

        # Initialize the radio
        if not self._driver.begin():
            raise RuntimeError(
                f"Failed to initialize SX1262 radio. Check:\n"
                f"  1. SPI enabled: ls /dev/spi*\n"
                f"  2. Wiring: RESET={self._reset_pin}, BUSY={self._busy_pin}, "
                f"DIO1={self._dio1_pin}, TXEN={self._txen_pin}, RXEN={self._rxen_pin}\n"
                f"  3. SPI: bus={self._spi_bus}, cs={self._spi_cs}"
            )

        # Map bandwidth Hz to driver constant
        bw_map = {
            125000: SX1262Driver.BW_125000,
            250000: SX1262Driver.BW_250000,
            500000: SX1262Driver.BW_500000,
        }
        bw = bw_map.get(self._bandwidth, SX1262Driver.BW_125000)

        # Map coding rate to driver constant
        cr_map = {
            5: SX1262Driver.CR_4_5,
            6: SX1262Driver.CR_4_6,
            7: SX1262Driver.CR_4_7,
            8: SX1262Driver.CR_4_8,
        }
        cr = cr_map.get(self._coding_rate, SX1262Driver.CR_4_5)

        # Map spreading factor to driver constant
        sf_map = {
            5: SX1262Driver.SF5,
            6: SX1262Driver.SF6,
            7: SX1262Driver.SF7,
            8: SX1262Driver.SF8,
            9: SX1262Driver.SF9,
            10: SX1262Driver.SF10,
            11: SX1262Driver.SF11,
            12: SX1262Driver.SF12,
        }
        sf = sf_map.get(self._spreading_factor, SX1262Driver.SF7)

        # Configure radio
        # Sync word 0x1424 is compatible with RFM9x sync word 0x12
        self._driver.configure(
            frequency_hz=int(self._frequency_mhz * 1_000_000),
            tx_power=self._tx_power,
            spreading_factor=sf,
            bandwidth=bw,
            coding_rate=cr,
            sync_word=0x1424,
            preamble_len=self._preamble_length,
        )

        compat_mode = "RFM9x-compatible" if self._rfm9x_compatible else "SX126x-native"
        logger.info(
            f"SX1262 initialized: {self._frequency_mhz} MHz, "
            f"SF{self._spreading_factor}, BW {self._bandwidth}Hz, "
            f"CR 4/{self._coding_rate}, TX {self._tx_power} dBm, "
            f"sync=0x1424, mode={compat_mode}"
        )

    def send(self, data: bytes) -> bool:
        """Send data over LoRa."""
        if self._driver is None:
            raise RuntimeError("Radio not initialized. Call init() first.")

        try:
            return self._driver.send(data)
        except Exception as e:
            logger.warning(f"Radio send failed: {e} (payload size: {len(data)} bytes)")
            return False

    def receive(self, timeout: float = 5.0) -> bytes | None:
        """Receive data from LoRa with timeout."""
        if self._driver is None:
            raise RuntimeError("Radio not initialized. Call init() first.")

        try:
            result = self._driver.receive(timeout_ms=int(timeout * 1000))
            if result is not None:
                data, rssi, snr = result
                self._last_rssi = rssi
                self._last_snr = snr
                return data
            return None
        except Exception as e:
            logger.warning(f"Radio receive failed: {e}")
            return None

    def get_last_rssi(self) -> int | None:
        """Get RSSI of last received packet."""
        return self._last_rssi

    def close(self) -> None:
        """Clean up radio resources."""
        if self._driver is not None:
            self._driver.close()
            self._driver = None
        self._last_rssi = None
        self._last_snr = None

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
        return self._last_snr
