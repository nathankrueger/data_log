"""BME280 temperature, pressure, and humidity sensor."""

from time import sleep

from .base import Sensor, c_to_f


class BME280TempPressureHumidity(Sensor):
    """Driver for BME280 temperature, pressure, and humidity sensor."""

    def __init__(self, smbus: int = 1):
        self._smbus = smbus
        self._bme = None
        self._bus = None

    def init(self) -> None:
        from bme280 import BME280
        from smbus2 import SMBus

        self._bus = SMBus(self._smbus)
        self._bme = BME280(i2c_dev=self._bus)
        # Flush the first junk reading
        self._bme.get_temperature()
        sleep(1.0)

    def read(self) -> tuple[float, float, float]:
        temp = c_to_f(self._bme.get_temperature())
        pressure = self._bme.get_pressure()
        humidity = self._bme.get_humidity()
        return (temp, pressure, humidity)

    def get_names(self) -> tuple[str, ...]:
        return ("Temperature", "Pressure", "Humidity")

    def get_units(self) -> tuple[str, ...]:
        return ("°F", "hPa", "%")

    def close(self) -> None:
        if self._bus:
            self._bus.close()
            self._bus = None
