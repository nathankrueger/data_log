"""Sensor base class and implementations."""

from abc import ABC, abstractmethod
from time import sleep

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


class BME280TempPressureHumidity(Sensor):
    def __init__(self, smbus: int = 1):
        self._smbus = smbus
        self._bme = None

    def init(self) -> None:
        bus = SMBus(self._smbus)
        self._bme = BME280(i2c_dev=bus)
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


class MMA8452Accelerometer(Sensor):
    """Driver for the MMA8452 3-axis accelerometer (SparkFun breakout)."""

    # I2C address (0x1D with SA0 high, 0x1C with SA0 low)
    DEFAULT_ADDRESS = 0x1D

    # Register addresses
    REG_STATUS = 0x00
    REG_OUT_X_MSB = 0x01
    REG_WHO_AM_I = 0x0D
    REG_XYZ_DATA_CFG = 0x0E
    REG_CTRL_REG1 = 0x2A

    # Device ID
    DEVICE_ID = 0x2A

    # Range settings
    RANGE_2G = 0x00
    RANGE_4G = 0x01
    RANGE_8G = 0x02

    def __init__(self, smbus: int = 1, address: int = DEFAULT_ADDRESS, range_g: int = RANGE_2G):
        self._smbus_num = smbus
        self._address = address
        self._range = range_g
        self._bus = None
        self._scale = 1.0

    def init(self) -> None:
        self._bus = SMBus(self._smbus_num)

        # Verify device ID
        device_id = self._bus.read_byte_data(self._address, self.REG_WHO_AM_I)
        if device_id != self.DEVICE_ID:
            raise RuntimeError(f"MMA8452 not found. Expected 0x{self.DEVICE_ID:02X}, got 0x{device_id:02X}")

        # Put into standby mode to configure
        self._set_standby(True)

        # Set range
        self._bus.write_byte_data(self._address, self.REG_XYZ_DATA_CFG, self._range)

        # Set scale factor based on range (12-bit resolution = 4096 counts for full range)
        if self._range == self.RANGE_2G:
            self._scale = 2.0 / 2048.0
        elif self._range == self.RANGE_4G:
            self._scale = 4.0 / 2048.0
        elif self._range == self.RANGE_8G:
            self._scale = 8.0 / 2048.0

        # Activate the sensor (ODR = 800Hz, normal mode)
        self._set_standby(False)
        sleep(0.1)

    def _set_standby(self, standby: bool) -> None:
        """Put device into standby or active mode."""
        ctrl = self._bus.read_byte_data(self._address, self.REG_CTRL_REG1)
        if standby:
            ctrl &= ~0x01
        else:
            ctrl |= 0x01
        self._bus.write_byte_data(self._address, self.REG_CTRL_REG1, ctrl)

    def read(self) -> tuple:
        """Return (x, y, z) acceleration in g's."""
        # Read 6 bytes starting from OUT_X_MSB
        data = self._bus.read_i2c_block_data(self._address, self.REG_OUT_X_MSB, 6)

        # Convert to 12-bit signed values (data is left-justified)
        x = self._convert_raw(data[0], data[1])
        y = self._convert_raw(data[2], data[3])
        z = self._convert_raw(data[4], data[5])

        return (x * self._scale, y * self._scale, z * self._scale)

    def _convert_raw(self, msb: int, lsb: int) -> int:
        """Convert raw 12-bit left-justified data to signed integer."""
        value = (msb << 8) | lsb
        value >>= 4  # Right-justify the 12-bit value
        # Sign extend if negative
        if value >= 2048:
            value -= 4096
        return value

    def get_names(self) -> tuple[str, ...]:
        return ("Accel X", "Accel Y", "Accel Z")

    def get_units(self) -> tuple[str, ...]:
        return ("g", "g", "g")
