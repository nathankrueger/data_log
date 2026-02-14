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


def _build_sensor_registry() -> tuple[dict[str, int], dict[int, str]]:
    """
    Build sensor class ID registry from all Sensor subclasses.

    Finds all concrete Sensor subclasses, sorts by name for deterministic
    ordering, and assigns sequential integer IDs.

    Returns:
        Tuple of (class_name -> id, id -> class_name) dicts
    """
    # Find all Sensor subclasses (excluding Sensor itself)
    sensor_classes = [
        cls.__name__
        for cls in Sensor.__subclasses__()
    ]
    # Sort alphabetically for deterministic IDs
    sensor_classes.sort()

    class_to_id = {name: i for i, name in enumerate(sensor_classes)}
    id_to_class = {i: name for i, name in enumerate(sensor_classes)}

    return class_to_id, id_to_class


# Build registry at import time from all loaded Sensor subclasses
SENSOR_CLASS_IDS, SENSOR_ID_CLASSES = _build_sensor_registry()


def get_sensor_class_id(class_name: str) -> int | None:
    """Get the integer ID for a sensor class name."""
    return SENSOR_CLASS_IDS.get(class_name)


def get_sensor_class_name(class_id: int) -> str | None:
    """Get the sensor class name for an integer ID."""
    return SENSOR_ID_CLASSES.get(class_id)
