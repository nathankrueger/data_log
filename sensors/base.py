"""Sensor base class and utilities."""

from abc import ABC, abstractmethod


def c_to_f(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return (c * 9.0 / 5.0) + 32


def transform_value(
    value: float,
    min_clip: float | None = None,
    max_clip: float | None = None,
    invert: bool = False,
) -> float:
    """Apply clip-then-invert transform to a single value.

    Clips value to [min_clip, max_clip], then optionally inverts within
    that range: result = min_clip + max_clip - clipped_value.
    """
    if invert and (min_clip is None or max_clip is None):
        raise ValueError("invert requires both min_clip and max_clip")
    if min_clip is not None:
        value = max(value, min_clip)
    if max_clip is not None:
        value = min(value, max_clip)
    if invert:
        value = min_clip + max_clip - value
    return value


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
