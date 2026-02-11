"""
Node package - outdoor sensor node that broadcasts via LoRa.

This package contains:
- data_log: Main node logic (sensor reading, LoRa broadcasting, command receiving)
"""

from node.data_log import main

__all__ = ["main"]
