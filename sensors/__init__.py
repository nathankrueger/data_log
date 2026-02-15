"""
Sensor drivers for data_log.

This package provides sensor abstractions and implementations for
various hardware sensors on Raspberry Pi.
"""

from .base import Sensor, c_to_f, transform_value
from .bme280_sensor import BME280TempPressureHumidity
from .mma8452_sensor import MMA8452Accelerometer
from .ads1115_sensor import ADS1115ADC

__all__ = [
    "Sensor",
    "c_to_f",
    "transform_value",
    "BME280TempPressureHumidity",
    "MMA8452Accelerometer",
    "ADS1115ADC",
    "SENSOR_CLASS_IDS",
    "SENSOR_ID_CLASSES",
    "get_sensor_class_id",
    "get_sensor_class_name",
]


# Manual sensor class ID registry.
# IDs are permanent — never reassign or reuse an ID.
# The HTCC AB01 firmware hardcodes these IDs, so changing
# existing assignments will break cross-device compatibility.
# When adding a new sensor, append it with the next available ID.
_SENSOR_ID_MAP: dict[str, int] = {
    "BME280TempPressureHumidity": 0,
    "MMA8452Accelerometer": 1,
    "ADS1115ADC": 2,
}

SENSOR_CLASS_IDS: dict[str, int] = dict(_SENSOR_ID_MAP)
SENSOR_ID_CLASSES: dict[int, str] = {v: k for k, v in _SENSOR_ID_MAP.items()}

assert len(SENSOR_CLASS_IDS) == len(SENSOR_ID_CLASSES), (
    f"Duplicate sensor class IDs in _SENSOR_ID_MAP: "
    f"{len(SENSOR_CLASS_IDS)} names but {len(SENSOR_ID_CLASSES)} unique IDs"
)


def get_sensor_class_id(class_name: str) -> int | None:
    """Get the integer ID for a sensor class name."""
    return SENSOR_CLASS_IDS.get(class_name)


def get_sensor_class_name(class_id: int) -> str | None:
    """Get the sensor class name for an integer ID."""
    return SENSOR_ID_CLASSES.get(class_id)
