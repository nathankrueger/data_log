"""
Backwards compatibility shim - imports from gateway.http_handler.

This module has moved to gateway/http_handler.py.
"""

from gateway.http_handler import (
    BW_CODE_MAP,
    BW_HZ_MAP,
    CommandHandler,
    CommandServer,
    GatewayParamDef,
    GatewayParamRegistry,
)

__all__ = [
    "BW_CODE_MAP",
    "BW_HZ_MAP",
    "CommandHandler",
    "CommandServer",
    "GatewayParamDef",
    "GatewayParamRegistry",
]
