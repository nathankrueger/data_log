"""Waveshare Core1262-868M LoRa radio implementation."""

import logging
import time

from .base import Radio

logger = logging.getLogger(__name__)


class SX1262Radio(Radio):
    """
    Waveshare Core1262-868M LoRa radio module implementation.

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

        self._sx1262 = None
        self._gpio = None
        self._last_rssi: int | None = None

    def init(self) -> None:
        """Initialize the SX1262 radio hardware."""
        try:
            from SX1262 import SX1262
        except ImportError:
            raise ImportError(
                "SX1262 library not found. Install with: pip install sx1262"
            )

        self._sx1262 = SX1262()

        # Begin with pin configuration
        # The library expects: begin(busId, csId, reset, busy, dio1, txen, rxen)
        ret = self._sx1262.begin(
            self._spi_bus,
            self._spi_cs,
            self._reset_pin,
            self._busy_pin,
            self._dio1_pin,
            self._txen_pin,
            self._rxen_pin,
        )
        if ret != 0:
            raise RuntimeError(f"Failed to initialize SX1262 radio (error: {ret})")

        # Set frequency
        ret = self._sx1262.setFrequency(int(self._frequency_mhz))
        if ret != 0:
            raise RuntimeError(f"Failed to set frequency (error: {ret})")

        # Bandwidth mapping for SX1262 library
        bw_map = {
            125000: 125.0,
            250000: 250.0,
            500000: 500.0,
        }
        bw_khz = bw_map.get(self._bandwidth, 125.0)

        # Set LoRa modulation parameters for RFM9x interoperability
        ret = self._sx1262.setLoRaModulation(
            self._spreading_factor,
            bw_khz,
            self._coding_rate,
        )
        if ret != 0:
            raise RuntimeError(f"Failed to set modulation params (error: {ret})")

        # Set packet parameters
        # Explicit header (variable length) for RFM9x compatibility
        ret = self._sx1262.setLoRaPacket(
            self._preamble_length,
            False,  # Explicit header (not implicit)
            255,    # Max payload length
            True,   # CRC enabled
            False,  # Standard IQ (not inverted)
        )
        if ret != 0:
            raise RuntimeError(f"Failed to set packet params (error: {ret})")

        # Set sync word for LoRa (0x12 = private network, matches RFM9x default)
        ret = self._sx1262.setSyncWord(0x12)
        if ret != 0:
            logger.warning(f"Failed to set sync word (error: {ret})")

        # Set TX power
        ret = self._sx1262.setTxPower(self._tx_power)
        if ret != 0:
            raise RuntimeError(f"Failed to set TX power (error: {ret})")

        logger.info(
            f"SX1262 initialized: {self._frequency_mhz} MHz, "
            f"SF{self._spreading_factor}, BW {self._bandwidth}Hz, "
            f"CR 4/{self._coding_rate}, TX {self._tx_power} dBm"
        )

    def send(self, data: bytes) -> bool:
        """Send data over LoRa."""
        if self._sx1262 is None:
            raise RuntimeError("Radio not initialized. Call init() first.")

        try:
            # The library handles RF switch control automatically
            ret = self._sx1262.send(list(data))
            return ret == len(data)
        except Exception as e:
            logger.warning(f"Radio send failed: {e} (payload size: {len(data)} bytes)")
            return False

    def receive(self, timeout: float = 5.0) -> bytes | None:
        """Receive data from LoRa with timeout."""
        if self._sx1262 is None:
            raise RuntimeError("Radio not initialized. Call init() first.")

        # Set receive mode with timeout
        timeout_ms = int(timeout * 1000)

        try:
            # Put radio in receive mode
            self._sx1262.setBlockingCallback(False)
            ret = self._sx1262.request(timeout_ms)

            if ret < 0:
                return None

            # Poll for received data
            start_time = time.monotonic()
            while (time.monotonic() - start_time) < timeout:
                status = self._sx1262.status()

                # Check if packet received
                if status == 1:  # RX done
                    # Read the received packet
                    data, err = self._sx1262.read()

                    if err == 0 and data:
                        # Store signal quality metrics
                        self._last_rssi = self._sx1262.packetRssi()
                        return bytes(data)
                    return None

                time.sleep(0.01)

            return None

        except Exception as e:
            logger.warning(f"Radio receive failed: {e}")
            return None

    def get_last_rssi(self) -> int | None:
        """Get RSSI of last received packet."""
        return self._last_rssi

    def close(self) -> None:
        """Clean up radio resources."""
        if self._sx1262 is not None:
            try:
                self._sx1262.standby()
            except Exception:
                pass
            self._sx1262 = None
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
        if self._sx1262 is None:
            return None
        try:
            return self._sx1262.snr()
        except Exception:
            return None
