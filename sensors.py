from smbus2 import SMBus
from bme280 import BME280
from time import sleep

def init_bme280(smbus: int = 1) -> BME280:
    # Initialise the BME280
    bus = SMBus(smbus)
    bme = BME280(i2c_dev=bus)

    # Flush the first junk reading
    bme.get_temperature()
    sleep(1.0)

    return bme
