"""
Backwards compatibility shim.

The HTTP server lives in gateway/web/ (WebServer, formerly CommandServer);
gateway parameters live in gateway/params.py.
"""

from gateway.http_handler import CommandServer
from gateway.params import GatewayParamDef, GatewayParamRegistry
from utils.radio_state import BW_CODE_MAP, BW_HZ_MAP

__all__ = [
    "BW_CODE_MAP",
    "BW_HZ_MAP",
    "CommandServer",
    "GatewayParamDef",
    "GatewayParamRegistry",
]
