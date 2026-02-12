"""
Gateway package - indoor gateway that collects sensor data via LoRa.

This package contains:
- server: Main gateway logic (LoRa transceiver, sensor collection)
- http_handler: HTTP server for command endpoints and gateway params
"""

from gateway.http_handler import CommandServer
from gateway.params import GatewayParamRegistry
from gateway.server import (
    CommandQueue,
    DiscoveryRequest,
    LoRaTransceiver,
    main,
    run_gateway,
)

__all__ = [
    "CommandQueue",
    "CommandServer",
    "DiscoveryRequest",
    "GatewayParamRegistry",
    "LoRaTransceiver",
    "main",
    "run_gateway",
]
