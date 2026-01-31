"""Sensor base class and utilities."""

from abc import ABC, abstractmethod


def c_to_f(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return (c * 9.0 / 5.0) + 32


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
