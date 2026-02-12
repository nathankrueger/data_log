"""
Gateway package - indoor gateway that collects sensor data via LoRa.

This package contains:
- command_queue: Command queue with ACK-based reliability
- sensor_collection: Sensor data collection and dashboard posting
- transceiver: LoRa transceiver thread
- http_handler: HTTP server for command endpoints and gateway params
- server: Main gateway orchestration and entry point
"""

from gateway.command_queue import CommandQueue, DiscoveryRequest, PendingCommand
from gateway.http_handler import CommandServer
from gateway.params import GatewayParamRegistry
from gateway.sensor_collection import (
    DashboardClient,
    LocalSensorReader,
    PendingPost,
    SensorDataCollector,
    get_sensor_class,
    instantiate_sensors,
)
from gateway.server import load_config, main, run_gateway
from gateway.transceiver import LoRaTransceiver

__all__ = [
    "CommandQueue",
    "CommandServer",
    "DashboardClient",
    "DiscoveryRequest",
    "GatewayParamRegistry",
    "LocalSensorReader",
    "LoRaTransceiver",
    "PendingCommand",
    "PendingPost",
    "SensorDataCollector",
    "get_sensor_class",
    "instantiate_sensors",
    "load_config",
    "main",
    "run_gateway",
]
