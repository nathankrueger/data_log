"""
data_log - Sensor data logging for Raspberry Pi.

This package provides a Sensor base class and implementations for
various hardware sensors. It can be used standalone for CSV logging
or imported by other projects (like rpi_server_cockpit) for integration.
"""

from .sensors import (
    Sensor,
    BME280TempPressureHumidity,
    MMA8452Accelerometer,
    c_to_f,
)

__all__ = [
    "Sensor",
    "BME280TempPressureHumidity",
    "MMA8452Accelerometer",
    "c_to_f",
]


def get_all_sensor_classes() -> list[type[Sensor]]:
    """
    Return all concrete Sensor subclasses defined in this package.

    This enables runtime discovery of sensors without hardcoding class names.
    New sensor classes added to sensors.py will be automatically discovered.

    Returns:
        List of Sensor subclass types (not instances)
    """
    from .sensors import Sensor
    import inspect
    from . import sensors as sensors_module

    sensor_classes = []
    for name, obj in inspect.getmembers(sensors_module, inspect.isclass):
        # Include classes that inherit from Sensor but aren't Sensor itself
        if issubclass(obj, Sensor) and obj is not Sensor:
            sensor_classes.append(obj)

    return sensor_classes
