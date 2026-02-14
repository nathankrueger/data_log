"""ADS1115 4-channel 16-bit ADC sensor."""

from enum import Enum

from .base import Sensor, transform_value


class ADS1115Gain(float, Enum):
    """Programmable gain amplifier settings for the ADS1115."""
    GAIN_2_3 = 2 / 3  # +/- 6.144V
    GAIN_1   = 1       # +/- 4.096V
    GAIN_2   = 2       # +/- 2.048V
    GAIN_4   = 4       # +/- 1.024V
    GAIN_8   = 8       # +/- 0.512V
    GAIN_16  = 16      # +/- 0.256V


class ADS1115Channel(int, Enum):
    """ADS1115 input channels."""
    A0 = 0
    A1 = 1
    A2 = 2
    A3 = 3


class ADS1115ADC(Sensor):
    """4-channel 16-bit ADC. Returns voltage readings for active channels."""

    DEFAULT_ADDRESS = 0x48

    def __init__(
        self,
        smbus: int = 1,
        address: int = DEFAULT_ADDRESS,
        channels: list[int] | None = None,
        names: tuple[str, ...] | None = None,
        units: tuple[str, ...] | None = None,
        gain: float = ADS1115Gain.GAIN_2_3,  # +/- 6.144V range (safest default)
        transforms: dict[str, dict] | None = None,
    ):
        self._smbus = smbus
        self._address = address

        # Parse active channels
        if channels is None:
            self._active_channels = list(ADS1115Channel)
        else:
            self._active_channels = [ADS1115Channel(ch) for ch in channels]
        n = len(self._active_channels)

        # Default/validate names
        if names is None:
            self._names = tuple(ch.name for ch in self._active_channels)
        else:
            if len(names) != n:
                raise ValueError(
                    f"names length {len(names)} doesn't match "
                    f"{n} active channels"
                )
            self._names = tuple(names)

        # Default/validate units
        if units is None:
            self._units = tuple("amplitude" for _ in self._active_channels)
        else:
            if len(units) != n:
                raise ValueError(
                    f"units length {len(units)} doesn't match "
                    f"{n} active channels"
                )
            self._units = tuple(units)

        if not isinstance(gain, ADS1115Gain):
            if isinstance(gain, str):
                _name_map = {g.name: g for g in ADS1115Gain}
                _name_map["2/3"] = ADS1115Gain.GAIN_2_3
                if gain not in _name_map:
                    raise ValueError(
                        f"Invalid gain '{gain}', must be one of: "
                        f"{list(_name_map.keys())}"
                    )
                gain = _name_map[gain]
            else:
                try:
                    gain = ADS1115Gain(gain)
                except ValueError:
                    valid = [g.value for g in ADS1115Gain]
                    raise ValueError(
                        f"Invalid gain {gain}, must be one of: {valid}"
                    )
        self._gain = gain
        self._ads = None
        self._i2c = None
        self._analog_inputs = None

        # Parse and validate per-channel transforms
        active_set = {ch.value for ch in self._active_channels}
        self._transforms: dict[int, dict] = {}
        if transforms:
            for key, t in transforms.items():
                ch = int(key)
                if ch not in active_set:
                    raise ValueError(
                        f"Transform channel {ch} is not in active channels "
                        f"{sorted(active_set)}"
                    )
                min_clip = t.get("min_clip")
                max_clip = t.get("max_clip")
                invert = t.get("invert", False)
                if invert and (min_clip is None or max_clip is None):
                    raise ValueError(
                        f"Channel {ch}: invert requires both min_clip and max_clip"
                    )
                self._transforms[ch] = {
                    "min_clip": min_clip,
                    "max_clip": max_clip,
                    "invert": invert,
                }

    def init(self) -> None:
        import board
        import busio
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn

        self._i2c = busio.I2C(board.SCL, board.SDA)
        self._ads = ADS.ADS1115(self._i2c, address=self._address)
        self._ads.gain = self._gain
        self._analog_inputs = [
            AnalogIn(self._ads, ch.value) for ch in self._active_channels
        ]

    def read(self) -> tuple:
        values = [ai.voltage for ai in self._analog_inputs]
        for i, ch in enumerate(self._active_channels):
            if ch.value in self._transforms:
                t = self._transforms[ch.value]
                values[i] = transform_value(values[i], **t)
        return tuple(values)

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
        self._analog_inputs = None
