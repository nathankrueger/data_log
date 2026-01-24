from smbus2 import SMBus
from bme280 import BME280

def c_to_f(c: float) -> float:
    return (c * 9.0/5.0) + 32

def init_bme280(smbus: int = 1) -> BME280:
    # Initialise the BME280
    bus = SMBus(smbus)
    bme = BME280(i2c_dev=bus)
    return bme
