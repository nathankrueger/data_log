"""
HTTP server for receiving commands from the dashboard.

Uses Python's built-in http.server module (no dependencies).
Dashboard sends POST requests to /command endpoint with JSON body.

Also provides gateway parameter endpoints:
  GET /gateway/params           - Get all gateway radio parameters
  GET /gateway/param/{name}     - Get single parameter value
  PUT /gateway/param/{name}?value=X - Set parameter and persist
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qs, urlparse

from utils.config_persistence import update_config_file

if TYPE_CHECKING:
    from radio import Radio

logger = logging.getLogger(__name__)


# =============================================================================
# Gateway Parameter Registry
# =============================================================================

# Bandwidth encoding: matches node convention (0/1/2 → Hz)
BW_HZ_MAP = {0: 125000, 1: 250000, 2: 500000}
BW_CODE_MAP = {v: k for k, v in BW_HZ_MAP.items()}


@dataclass
class GatewayParamDef:
    """Definition of a gateway parameter."""

    name: str
    getter: Callable[[], int | float | str]
    setter: Callable[[Any], None] | None = None  # None = read-only
    config_key: str | None = None  # Key path for persistence (e.g., "lora.spreading_factor")
    min_val: int | float | None = None
    max_val: int | float | None = None
    value_type: type = int  # int, float, str


class GatewayParamRegistry:
    """Registry for gateway parameters with get/set and persistence support."""

    def __init__(
        self,
        radio: "Radio",
        config: dict,
        config_path: str,
        node_id: str,
    ):
        self._radio = radio
        self._config = config
        self._config_path = config_path
        self._node_id = node_id
        self._params = self._build_params()

    def _build_params(self) -> dict[str, GatewayParamDef]:
        """Build the parameter definitions."""
        radio = self._radio
        lora_config = self._config.get("lora", {})

        params = [
            GatewayParamDef(
                name="sf",
                getter=lambda: radio.spreading_factor,
                setter=lambda v: setattr(radio, "spreading_factor", int(v)),
                config_key="lora.spreading_factor",
                min_val=7,
                max_val=12,
            ),
            GatewayParamDef(
                name="bw",
                getter=lambda: BW_CODE_MAP.get(radio.signal_bandwidth, 0),
                setter=lambda v: setattr(radio, "signal_bandwidth", BW_HZ_MAP[int(v)]),
                config_key="lora.signal_bandwidth",
                min_val=0,
                max_val=2,
            ),
            GatewayParamDef(
                name="txpwr",
                getter=lambda: radio.tx_power,
                setter=lambda v: setattr(radio, "tx_power", int(v)),
                config_key="lora.tx_power",
                min_val=5,
                max_val=23,
            ),
            GatewayParamDef(
                name="nodeid",
                getter=lambda: self._node_id,
                setter=None,  # Read-only
                value_type=str,
            ),
            GatewayParamDef(
                name="n2g_freq",
                getter=lambda: lora_config.get("n2g_frequency_mhz", 915.0),
                setter=None,  # Frequency changes require restart
                value_type=float,
            ),
            GatewayParamDef(
                name="g2n_freq",
                getter=lambda: lora_config.get("g2n_frequency_mhz", 915.5),
                setter=None,  # Frequency changes require restart
                value_type=float,
            ),
        ]
        return {p.name: p for p in params}

    def get_all(self) -> dict[str, Any]:
        """Get all parameter values."""
        return {name: p.getter() for name, p in sorted(self._params.items())}

    def get(self, name: str) -> tuple[Any | None, str | None]:
        """Get a parameter value. Returns (value, None) or (None, error_msg)."""
        p = self._params.get(name)
        if p is None:
            return None, f"unknown param: {name}"
        return p.getter(), None

    def set(self, name: str, value_str: str) -> tuple[Any | None, str | None]:
        """
        Set a parameter value and persist to config.

        Returns (new_value, None) on success or (None, error_msg) on failure.
        """
        p = self._params.get(name)
        if p is None:
            return None, f"unknown param: {name}"
        if p.setter is None:
            return None, f"read-only: {name}"

        # Parse value
        try:
            if p.value_type is int:
                val = int(value_str)
            elif p.value_type is float:
                val = float(value_str)
            else:
                val = value_str
        except ValueError:
            return None, f"invalid value: {value_str}"

        # Range check
        if p.min_val is not None and val < p.min_val:
            return None, f"range: {p.min_val}..{p.max_val}"
        if p.max_val is not None and val > p.max_val:
            return None, f"range: {p.min_val}..{p.max_val}"

        # Apply the change
        p.setter(val)

        # Persist to config file if there's a config key
        if p.config_key and self._config_path:
            # For bandwidth, persist the Hz value not the code
            if name == "bw":
                persist_val = BW_HZ_MAP[int(val)]
            else:
                persist_val = val
            update_config_file(self._config_path, {p.config_key: persist_val})

        return p.getter(), None


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

        Patterns:
          GET /discover[?retries=N]       - Discover all reachable nodes
          GET /gateway/params             - Get all gateway parameters
          GET /gateway/param/{name}       - Get single gateway parameter
          GET /{cmd}/{node_id}?a=arg1     - Send command, wait for response
        """
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")

        # Handle /discover endpoint
        if path == "discover":
            self._handle_discover(parsed)
            return

        # Handle /gateway/params - get all gateway parameters
        if path == "gateway/params":
            self._handle_gateway_params_get_all()
            return

        # Handle /gateway/param/{name} - get single gateway parameter
        if path.startswith("gateway/param/"):
            param_name = path[len("gateway/param/"):]
            self._handle_gateway_param_get(param_name)
            return

        parts = path.split("/")

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
            # Non-empty dict = payload from node; empty dict = ACK with no payload
            result = response if response else {"status": "acked"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))
        else:
            # Cancel the command so it doesn't block subsequent commands
            self.server.command_queue.cancel(command_id)  # type: ignore
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "timeout",
                "message": f"No response from node '{node_id}' within 10 seconds",
            }).encode("utf-8"))

    def _handle_discover(self, parsed) -> None:
        """Handle GET /discover — discover all reachable nodes via broadcast ping."""
        transceiver = getattr(self.server, "transceiver", None)
        if transceiver is None:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "unavailable",
                "message": "LoRa transceiver not running",
            }).encode("utf-8"))
            return

        # Parse optional query params
        query = parse_qs(parsed.query)
        disc_config = getattr(self.server, "discovery_config", {})

        retries = int(
            query.get("retries", [disc_config.get("discovery_retries", 30)])[0]
        )

        # Import at runtime to avoid circular import
        # This will be gateway.server after the move
        try:
            from gateway.server import DiscoveryRequest
        except ImportError:
            from gateway_server import DiscoveryRequest

        request = DiscoveryRequest(
            retries=retries,
            initial_retry_ms=disc_config.get("initial_retry_ms", 500),
            max_retry_ms=disc_config.get("max_retry_ms", 5000),
            retry_multiplier=disc_config.get("retry_multiplier", 1.5),
            done=threading.Event(),
        )

        # Submit to transceiver
        accepted = transceiver.request_discovery(request)
        if not accepted:
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "conflict",
                "message": "Discovery already in progress",
            }).encode("utf-8"))
            return

        # Compute expected discovery duration from retry parameters
        expected_ms = 0.0
        delay = float(request.initial_retry_ms)
        for _ in range(retries):
            expected_ms += delay
            delay = min(delay * request.retry_multiplier, float(request.max_retry_ms))
        wait_timeout = (expected_ms / 1000.0) + 10.0  # add buffer

        logger.info(
            f"Discovery requested ({retries} broadcasts, "
            f"timeout={wait_timeout:.0f}s), waiting for completion..."
        )

        # Block until discovery completes
        completed = request.done.wait(timeout=wait_timeout)

        if not completed:
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "timeout",
                "message": "Discovery did not complete in time",
            }).encode("utf-8"))
            return

        if request.error:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "discovery_error",
                "message": request.error,
            }).encode("utf-8"))
            return

        # Success
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "nodes": request.nodes,
            "count": len(request.nodes),
        }).encode("utf-8"))

    def do_PUT(self) -> None:
        """
        Handle PUT requests for setting gateway parameters.

        Pattern:
          PUT /gateway/param/{name} with JSON body {"value": X}
        """
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")

        # Handle /gateway/param/{name} with JSON body
        if path.startswith("gateway/param/"):
            param_name = path[len("gateway/param/"):]

            # Read and parse JSON body
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

            if "value" not in data:
                self.send_error(400, "Missing 'value' field in JSON body")
                return

            self._handle_gateway_param_set(param_name, str(data["value"]))
            return

        self.send_error(404, "Not Found")

    def _handle_gateway_params_get_all(self) -> None:
        """Handle GET /gateway/params - get all gateway parameters."""
        registry = getattr(self.server, "gateway_params", None)
        if registry is None:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "unavailable",
                "message": "Gateway parameter registry not initialized",
            }).encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(registry.get_all()).encode("utf-8"))

    def _handle_gateway_param_get(self, name: str) -> None:
        """Handle GET /gateway/param/{name} - get single gateway parameter."""
        registry = getattr(self.server, "gateway_params", None)
        if registry is None:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "unavailable",
                "message": "Gateway parameter registry not initialized",
            }).encode("utf-8"))
            return

        value, error = registry.get(name)
        if error:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": error}).encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({name: value}).encode("utf-8"))

    def _handle_gateway_param_set(self, name: str, value: str) -> None:
        """Handle PUT /gateway/param/{name}?value=X - set gateway parameter."""
        registry = getattr(self.server, "gateway_params", None)
        if registry is None:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "unavailable",
                "message": "Gateway parameter registry not initialized",
            }).encode("utf-8"))
            return

        new_value, error = registry.set(name, value)
        if error:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": error}).encode("utf-8"))
            return

        logger.info(f"Gateway param '{name}' set to {new_value}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({name: new_value}).encode("utf-8"))

    def log_message(self, format: str, *args) -> None:
        """Suppress default HTTP logging, use our logger instead."""
        pass


class CommandServer(threading.Thread):
    """
    HTTP server thread for receiving commands from dashboard.

    Runs as a daemon thread, listening for POST /command requests.
    Commands are validated and placed on a CommandQueue for the LoRa
    transceiver to send with ACK-based reliability.

    Also provides gateway parameter endpoints for runtime tuning.

    Example:
        command_queue = CommandQueue(max_size=128)
        server = CommandServer(port=5001, command_queue=command_queue)
        server.start()

        # Commands are sent with retry until ACK received
    """

    def __init__(
        self,
        port: int,
        command_queue,
        discovery_config: dict | None = None,
        radio: "Radio | None" = None,
        config: dict | None = None,
        config_path: str | None = None,
        node_id: str = "",
    ):
        """
        Initialize the command server.

        Args:
            port: TCP port to listen on
            command_queue: CommandQueue for reliable command delivery
            discovery_config: Config for node discovery (retries, backoff params)
            radio: Radio instance for gateway parameter access
            config: Gateway configuration dict
            config_path: Path to config file for persistence
            node_id: Gateway node ID
        """
        super().__init__(daemon=True, name="CommandServer")
        self.port = port
        self.command_queue = command_queue
        self.discovery_config = discovery_config or {}
        self.transceiver = None  # Set later via set_transceiver()
        self._server: HTTPServer | None = None

        # Gateway parameter registry
        self.gateway_params: GatewayParamRegistry | None = None
        if radio is not None and config is not None and config_path is not None:
            self.gateway_params = GatewayParamRegistry(
                radio=radio,
                config=config,
                config_path=config_path,
                node_id=node_id,
            )

    def set_transceiver(self, transceiver) -> None:
        """Set the transceiver reference for discovery support."""
        self.transceiver = transceiver
        if self._server:
            self._server.transceiver = transceiver  # type: ignore

    def set_gateway_params(
        self,
        radio: "Radio",
        config: dict,
        config_path: str,
        node_id: str,
    ) -> None:
        """Set up gateway parameter registry (call after radio is initialized)."""
        self.gateway_params = GatewayParamRegistry(
            radio=radio,
            config=config,
            config_path=config_path,
            node_id=node_id,
        )
        if self._server:
            self._server.gateway_params = self.gateway_params  # type: ignore

    def run(self) -> None:
        """Run the HTTP server (called by Thread.start())."""
        self._server = HTTPServer(("0.0.0.0", self.port), CommandHandler)
        self._server.command_queue = self.command_queue  # type: ignore
        self._server.discovery_config = self.discovery_config  # type: ignore
        self._server.transceiver = self.transceiver  # type: ignore
        self._server.gateway_params = self.gateway_params  # type: ignore
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
