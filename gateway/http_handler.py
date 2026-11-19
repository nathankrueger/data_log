"""
Backwards-compatibility shim.

The HTTP server moved to the gateway.web package (gateway/web/server.py),
with the command endpoints in gateway/web/routes_commands.py.
"""

from gateway.web.server import WebServer

CommandServer = WebServer

__all__ = ["CommandServer", "WebServer"]
