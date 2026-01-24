"""Sensor base class and implementations."""

from abc import ABC, abstractmethod
from time import sleep
from typing import Union

from bme280 import BME280
from smbus2 import SMBus


def c_to_f(c: float) -> float:
    return (c * 9.0 / 5.0) + 32


class Sensor(ABC):
    @abstractmethod
    def init(self) -> None:
        """Initialize the sensor."""
        pass

    @abstractmethod
    def read(self) -> Union[int, float]:
        """Return the current sensor value."""
        pass

    @abstractmethod
    def get_name(self) -> str:
        """Return the sensor name."""
        pass

    @abstractmethod
    def get_units(self) -> str:
        """Return the units of measurement."""
        pass


class BME280Temperature(Sensor):
    def __init__(self, smbus: int = 1):
        self._smbus = smbus
        self._bme = None

    def init(self) -> None:
        bus = SMBus(self._smbus)
        self._bme = BME280(i2c_dev=bus)
        # Flush the first junk reading
        self._bme.get_temperature()
        sleep(1.0)

    def read(self) -> float:
        return c_to_f(self._bme.get_temperature())

    def get_name(self) -> str:
        return "BME280 Temperature"

    def get_units(self) -> str:
        return "°F"
