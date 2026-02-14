"""ADS1115 4-channel 16-bit ADC sensor."""

from .base import Sensor


class ADS1115ADC(Sensor):
    """4-channel 16-bit ADC. Returns voltage readings for all channels."""

    DEFAULT_ADDRESS = 0x48

    def __init__(
        self,
        smbus: int = 1,
        address: int = DEFAULT_ADDRESS,
        units: tuple[str, str, str, str] = ("amplitude", "amplitude", "amplitude", "amplitude"),
        gain: float = 2/3,  # +/- 6.144V range (safest default)
        names: tuple[str, str, str, str] = ("A0", "A1", "A2", "A3"),
    ):
        self._smbus = smbus
        self._address = address
        self._units = units
        self._gain = gain
        self._names = names
        self._ads = None
        self._i2c = None
        self._channels = None

    def init(self) -> None:
        import board
        import busio
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn

        self._i2c = busio.I2C(board.SCL, board.SDA)
        self._ads = ADS.ADS1115(self._i2c, address=self._address)
        self._ads.gain = self._gain

        self._channels = [
            AnalogIn(self._ads, ADS.P0),
            AnalogIn(self._ads, ADS.P1),
            AnalogIn(self._ads, ADS.P2),
            AnalogIn(self._ads, ADS.P3),
        ]

    def read(self) -> tuple[float, float, float, float]:
        return tuple(ch.voltage for ch in self._channels)

    def get_names(self) -> tuple[str, ...]:
        return self._names

    def get_units(self) -> tuple[str, ...]:
        return self._units

    def get_precision(self) -> int:
        return 4  # 16-bit ADC benefits from extra precision

    def close(self) -> None:
        if self._i2c:
            self._i2c.deinit()
            self._i2c = None
        self._ads = None
        self._channels = None
