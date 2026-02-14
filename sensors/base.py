"""Sensor base class and utilities."""

from abc import ABC, abstractmethod


def c_to_f(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return (c * 9.0 / 5.0) + 32


def transform_value(
    value: float,
    raw_min: float,
    raw_max: float,
    invert: bool = False,
) -> float:
    """Clip and normalize a value from [raw_min, raw_max] to [0.0, 1.0].

    If invert is True, output is flipped: 1.0 - normalized.
    """
    value = max(value, raw_min)
    value = min(value, raw_max)
    normalized = (value - raw_min) / (raw_max - raw_min)
    if invert:
        normalized = 1.0 - normalized
    return normalized


class Sensor(ABC):
    """Abstract base class for all sensors."""

    @abstractmethod
    def init(self) -> None:
        """Initialize the sensor."""
        pass

    @abstractmethod
    def read(self) -> tuple:
        """Return the current sensor value(s) as a tuple."""
        pass

    @abstractmethod
    def get_names(self) -> tuple[str, ...]:
        """Return the sensor name(s). Tuple length matches read() output count."""
        pass

    @abstractmethod
    def get_units(self) -> tuple[str, ...]:
        """Return the units of measurement. Tuple length matches read() output count."""
        pass

    def get_precision(self) -> int:
        """Return the number of decimal places for float values. Default is 3."""
        return 3

    def close(self) -> None:
        """Release hardware resources. Override if sensor uses I2C/SPI/GPIO."""
        pass
