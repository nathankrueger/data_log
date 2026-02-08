"""
HTTP server for receiving commands from the dashboard.

Uses Python's built-in http.server module (no dependencies).
Dashboard sends POST requests to /command endpoint with JSON body.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from gateway_server import CommandQueue

logger = logging.getLogger(__name__)


class CommandHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler for command endpoint.

    Expects POST /command with JSON body:
    {
        "cmd": "command_name",
        "args": ["arg1", "arg2"],  // optional, defaults to []
        "node_id": "node_123"      // optional, defaults to "" (broadcast)
    }
    """

    def do_POST(self) -> None:
        """Handle POST requests."""
        if self.path != "/command":
            self.send_error(404, "Not Found")
            return

        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_error(400, "Empty request body")
            return

        try:
            body = self.rfile.read(content_length)
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self.send_error(400, f"Invalid JSON: {e}")
            return

        # Validate and extract fields
        cmd = data.get("cmd")
        if not cmd or not isinstance(cmd, str):
            self.send_error(400, "Missing or invalid 'cmd' field")
            return

        args = data.get("args", [])
        if not isinstance(args, list):
            self.send_error(400, "'args' must be a list")
            return

        # Ensure all args are strings
        args = [str(arg) for arg in args]

        node_id = data.get("node_id", "")
        if not isinstance(node_id, str):
            self.send_error(400, "'node_id' must be a string")
            return

        # Queue the command for LoRa transmission with ACK-based delivery
        command_id = self.server.command_queue.add(cmd, args, node_id)  # type: ignore

        if command_id is None:
            self.send_error(503, "Command queue full")
            return

        target = node_id if node_id else "broadcast"
        logger.info(f"Queued command '{cmd}' for {target} (id: {command_id})")

        # Send success response with command_id for tracking
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = json.dumps({
            "status": "queued",
            "command_id": command_id,
            "cmd": cmd,
            "target": target,
        })
        self.wfile.write(response.encode("utf-8"))

    def do_GET(self) -> None:
        """
        Handle GET requests for commands that return responses.

        Pattern: GET /{cmd}/{node_id}?a=arg1&a=arg2

        Queues the command, waits for ACK with payload, returns payload.
        The payload content is opaque to the gateway.
        """
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")

        if len(parts) != 2:
            self.send_error(400, "Expected: /{cmd}/{node_id}")
            return

        cmd, node_id = parts
        if not cmd or not node_id:
            self.send_error(400, "Both cmd and node_id are required")
            return

        # Parse query params for args (e.g., ?a=foo&a=bar)
        query = parse_qs(parsed.query)
        args = query.get("a", [])  # List of arg values

        # Queue the command
        command_id = self.server.command_queue.add(cmd, args, node_id)  # type: ignore
        if command_id is None:
            self.send_error(503, "Command queue full")
            return

        logger.info(f"Queued '{cmd}' for {node_id}, waiting for response...")

        # Wait for response with timeout
        response = self.server.command_queue.wait_for_response(  # type: ignore
            command_id, timeout=10.0
        )

        if response is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))
        else:
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "timeout",
                "message": f"No response from node '{node_id}' within 10 seconds",
            }).encode("utf-8"))

    def log_message(self, format: str, *args) -> None:
        """Suppress default HTTP logging, use our logger instead."""
        pass


class CommandServer(threading.Thread):
    """
    HTTP server thread for receiving commands from dashboard.

    Runs as a daemon thread, listening for POST /command requests.
    Commands are validated and placed on a CommandQueue for the LoRa
    transceiver to send with ACK-based reliability.

    Example:
        command_queue = CommandQueue(max_size=128)
        server = CommandServer(port=5001, command_queue=command_queue)
        server.start()

        # Commands are sent with retry until ACK received
    """

    def __init__(self, port: int, command_queue: "CommandQueue"):
        """
        Initialize the command server.

        Args:
            port: TCP port to listen on
            command_queue: CommandQueue for reliable command delivery
        """
        super().__init__(daemon=True, name="CommandServer")
        self.port = port
        self.command_queue = command_queue
        self._server: HTTPServer | None = None

    def run(self) -> None:
        """Run the HTTP server (called by Thread.start())."""
        self._server = HTTPServer(("0.0.0.0", self.port), CommandHandler)
        self._server.command_queue = self.command_queue  # type: ignore
        logger.info(f"Command server listening on port {self.port}")

        try:
            self._server.serve_forever()
        except Exception as e:
            logger.error(f"Command server error: {e}")

    def stop(self) -> None:
        """Stop the HTTP server."""
        if self._server:
            logger.info("Stopping command server")
            self._server.shutdown()
            self._server = None
