"""RFM9x LoRa radio implementation."""

from .base import Radio


class RFM9xRadio(Radio):
    """
    Adafruit RFM9x LoRa radio implementation.

    Wiring (RFM9x to Pi):
        VIN  -> 3.3V
        GND  -> GND
        SCK  -> GPIO 11 (SPI0 SCLK)
        MISO -> GPIO 9  (SPI0 MISO)
        MOSI -> GPIO 10 (SPI0 MOSI)
        CS   -> Configurable GPIO (default: GPIO 24)
        RST  -> Configurable GPIO (default: GPIO 25)
    """

    def __init__(
        self,
        frequency_mhz: float = 915.0,
        tx_power: int = 23,
        cs_pin: int = 24,
        reset_pin: int = 25,
    ):
        """
        Initialize RFM9x radio configuration.

        Args:
            frequency_mhz: Radio frequency (915.0 for US, 868.0 for EU)
            tx_power: Transmit power in dBm (5-23)
            cs_pin: GPIO pin number for chip select
            reset_pin: GPIO pin number for reset
        """
        self._frequency_mhz = frequency_mhz
        self._tx_power = tx_power
        self._cs_pin = cs_pin
        self._reset_pin = reset_pin

        self._rfm9x = None
        self._spi = None
        self._cs = None
        self._reset = None

    def init(self) -> None:
        """Initialize the RFM9x radio hardware."""
        import board
        import busio
        import digitalio
        import adafruit_rfm9x

        # Map pin numbers to board pins
        cs_board_pin = getattr(board, f"D{self._cs_pin}")
        reset_board_pin = getattr(board, f"D{self._reset_pin}")

        self._spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)
        self._cs = digitalio.DigitalInOut(cs_board_pin)
        self._reset = digitalio.DigitalInOut(reset_board_pin)

        self._rfm9x = adafruit_rfm9x.RFM9x(
            self._spi, self._cs, self._reset, self._frequency_mhz
        )
        self._rfm9x.tx_power = self._tx_power

    def send(self, data: bytes) -> bool:
        """Send data over LoRa."""
        if self._rfm9x is None:
            raise RuntimeError("Radio not initialized. Call init() first.")
        try:
            self._rfm9x.send(data)
            return True
        except Exception:
            return False

    def receive(self, timeout: float = 5.0) -> bytes | None:
        """Receive data from LoRa with timeout."""
        if self._rfm9x is None:
            raise RuntimeError("Radio not initialized. Call init() first.")
        return self._rfm9x.receive(timeout=timeout)

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
        if self._spi:
            self._spi.deinit()
            self._spi = None
        self._cs = None
        self._reset = None

    @property
    def frequency_mhz(self) -> float:
        """Get the configured frequency."""
        return self._frequency_mhz

    @property
    def tx_power(self) -> int:
        """Get the configured transmit power."""
        return self._tx_power


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
