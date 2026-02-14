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
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from utils.config_persistence import update_config_file
from utils.radio_state import BW_HZ_MAP

logger = logging.getLogger(__name__)
cmd_logger = logging.getLogger("cmd_debug")

# Retry count for fire-and-forget commands (no_wait=1)
# Used for rcfg_radio where ACK is unreliable after radio params change
NO_WAIT_MAX_RETRIES = 2


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

    def setup(self) -> None:
        """Set socket timeout to prevent indefinite blocking on client disconnect."""
        super().setup()
        # 30 second timeout - long enough for normal operations,
        # short enough to recover from dead clients
        self.request.settimeout(30.0)

    def do_POST(self) -> None:
        """Handle POST requests."""
        if self.path == "/gateway/restart":
            self._handle_restart()
            return

        if self.path == "/gateway/rcfg_radio":
            self._handle_rcfg_radio()
            return

        if self.path == "/gateway/savecfg":
            self._handle_savecfg()
            return

        if self.path == "/gateway/flush_commands":
            self._handle_flush_commands()
            return

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

        expected_acks = data.get("expected_acks", 1)
        if not isinstance(expected_acks, int) or expected_acks < 1:
            self.send_error(400, "'expected_acks' must be a positive integer")
            return

        # Queue the command for LoRa transmission with ACK-based delivery
        command_id = self.server.command_queue.add(  # type: ignore
            cmd, args, node_id, expected_acks=expected_acks
        )

        if command_id is None:
            self.send_error(503, "Command queue full")
            return

        target = node_id if node_id else "broadcast"
        logger.info(
            f"Queued command '{cmd}' for {target} "
            f"(id: {command_id}, expected_acks: {expected_acks})"
        )

        # Send success response with command_id for tracking
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = json.dumps({
            "status": "queued",
            "command_id": command_id,
            "cmd": cmd,
            "target": target,
            "expected_acks": expected_acks,
        })
        self.wfile.write(response.encode("utf-8"))

    def do_GET(self) -> None:
        """
        Handle GET requests for commands that return responses.

        Patterns:
          GET /discover[?retries=N]       - Discover all reachable nodes
          GET /gateway/params             - Get all gateway parameters
          GET /gateway/param/{name}       - Get single gateway parameter
          GET /{cmd}?expected_acks=N&a=X  - Broadcast command, wait for N ACKs
          GET /{cmd}/{node_id}?a=arg1     - Send command to node, wait for response
        """
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")

        # Handle /discover endpoint
        if path == "discover":
            self._handle_discover(parsed)
            return

        # Handle /gateway/uptime - get gateway uptime
        if path == "gateway/uptime":
            self._handle_uptime()
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

        # Handle broadcast wait: GET /{cmd}?expected_acks=N (single path segment)
        if len(parts) == 1 and parts[0]:
            cmd = parts[0]
            query = parse_qs(parsed.query)
            expected_acks = int(query.get("expected_acks", ["1"])[0])
            args = query.get("a", [])

            command_id = self.server.command_queue.add(  # type: ignore
                cmd, args, node_id="", expected_acks=expected_acks
            )
            if command_id is None:
                self.send_error(503, "Command queue full")
                return

            logger.info(
                f"Queued broadcast '{cmd}' (expected_acks={expected_acks}), "
                f"waiting for response..."
            )

            wait_timeout = self.server.command_queue.wait_timeout  # type: ignore
            response = self.server.command_queue.wait_for_response(  # type: ignore
                command_id, timeout=wait_timeout
            )

            if response is not None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode("utf-8"))
            else:
                # Timeout - return partial results
                partial = self.server.command_queue.get_partial_acks(command_id)  # type: ignore
                self.server.command_queue.cancel(command_id)  # type: ignore

                self.send_response(504)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if partial:
                    result = {
                        "error": "timeout",
                        "expected_acks": partial["expected_acks"],
                        "acked_nodes": partial["acked_nodes"],
                        "responses": partial["responses"],
                        "missing": partial["expected_acks"] - len(partial["acked_nodes"]),
                    }
                else:
                    result = {
                        "error": "timeout",
                        "message": f"Broadcast timed out after {wait_timeout} seconds",
                    }
                self.wfile.write(json.dumps(result).encode("utf-8"))
            return

        if len(parts) != 2:
            self.send_error(400, "Expected: /{cmd}/{node_id} or /{cmd}?expected_acks=N")
            return

        cmd, node_id = parts
        if not cmd or not node_id:
            self.send_error(400, "Both cmd and node_id are required")
            return

        # Parse query params for args (e.g., ?a=foo&a=bar)
        query = parse_qs(parsed.query)
        args = query.get("a", [])  # List of arg values

        # Check for fire-and-forget mode (no_wait=1)
        # Used for rcfg_radio where ACK is unreliable after radio params change
        no_wait = query.get("no_wait", ["0"])[0] == "1"
        cmd_logger.debug(
            "HTTP_GET cmd=%s node=%s query=%s no_wait=%s",
            cmd, node_id, query, no_wait
        )

        if no_wait:
            # Fire-and-forget: use reduced retries, return immediately
            cmd_logger.debug(
                "QUEUE_ADD cmd=%s node=%s max_retries=%d (no_wait)",
                cmd, node_id, NO_WAIT_MAX_RETRIES
            )
            command_id = self.server.command_queue.add(  # type: ignore
                cmd, args, node_id, max_retries=NO_WAIT_MAX_RETRIES
            )
            if command_id is None:
                self.send_error(503, "Command queue full")
                return
            logger.info(f"Queued '{cmd}' for {node_id} (no_wait mode)")
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "queued",
                "id": command_id,
            }).encode("utf-8"))
            return

        # Queue the command (normal mode - wait for response)
        command_id = self.server.command_queue.add(cmd, args, node_id)  # type: ignore
        if command_id is None:
            self.send_error(503, "Command queue full")
            return

        logger.info(f"Queued '{cmd}' for {node_id}, waiting for response...")

        # Wait for response with timeout
        wait_timeout = self.server.command_queue.wait_timeout  # type: ignore
        response = self.server.command_queue.wait_for_response(  # type: ignore
            command_id, timeout=wait_timeout
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
                "message": f"No response from node '{node_id}' within {wait_timeout} seconds",
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

        from gateway.command_queue import DiscoveryRequest

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

    def _handle_uptime(self) -> None:
        """Handle GET /gateway/uptime - get gateway uptime."""
        gateway_state = getattr(self.server, "gateway_state", None)
        if gateway_state is None:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "unavailable",
                "message": "Gateway state not initialized",
            }).encode("utf-8"))
            return

        uptime_seconds = time.time() - gateway_state.start_time
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "uptime_seconds": uptime_seconds,
        }).encode("utf-8"))

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

    def _handle_rcfg_radio(self) -> None:
        """Handle POST /gateway/rcfg_radio - apply staged radio config (no persist).

        Config is applied by the LoRaTransceiver thread to avoid SPI contention.
        This handler waits for the transceiver to apply the pending config.
        """
        gateway_state = getattr(self.server, "gateway_state", None)
        if gateway_state is None or gateway_state.radio_state is None:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "unavailable",
                "message": "Radio state not initialized",
            }).encode("utf-8"))
            return

        radio_state = gateway_state.radio_state

        # Check if there's anything to apply
        if not radio_state.has_pending():
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"r": "nothing"}).encode("utf-8"))
            return

        # Wait for LoRaTransceiver to apply the pending config
        # (it checks has_pending() at the top of each loop iteration)
        success, applied = radio_state.wait_for_apply(timeout=1.0)

        if not success:
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "timeout",
                "message": "Timed out waiting for config to be applied",
            }).encode("utf-8"))
            return

        # NO persist here - savecfg handles persistence
        logger.info(f"Gateway rcfg_radio applied: {', '.join(applied)}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"r": ", ".join(applied)}).encode("utf-8"))

    def _handle_savecfg(self) -> None:
        """Handle POST /gateway/savecfg - persist ALL current params to config file."""
        gateway_state = getattr(self.server, "gateway_state", None)
        registry = getattr(self.server, "gateway_params", None)
        if gateway_state is None or registry is None:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "unavailable",
                "message": "Gateway state not initialized",
            }).encode("utf-8"))
            return

        config_path = gateway_state.config_path
        if not config_path:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "unavailable",
                "message": "No config path configured",
            }).encode("utf-8"))
            return

        # Build updates from current values of all writable params
        updates = {}
        for name, p in registry._params.items():
            if p.config_key and p.setter is not None:  # writable params with config_key
                val = p.getter()  # Current runtime value
                # Convert for persistence
                if name == "bw":
                    val = BW_HZ_MAP.get(val, val)  # Store Hz, not code
                updates[p.config_key] = val

        if not updates:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"r": "unchanged"}).encode("utf-8"))
            return

        update_config_file(config_path, updates)
        logger.info(f"Gateway savecfg: persisted {len(updates)} params")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"r": "saved"}).encode("utf-8"))

    def _handle_flush_commands(self) -> None:
        """Handle POST /gateway/flush_commands - clear all pending commands."""
        count = self.server.command_queue.flush()  # type: ignore
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"flushed": count}).encode("utf-8"))

    def _handle_restart(self) -> None:
        """Handle POST /gateway/restart - restart gateway service."""
        # Send response before initiating restart
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "restarting"}).encode("utf-8"))

        # Spawn detached process: wait 1s to ensure HTTP response completes, then restart
        # start_new_session=True detaches from parent so it survives our death
        logger.info("Restart endpoint triggered - spawning systemctl restart in 1s")
        subprocess.Popen(
            ["sh", "-c", "sleep 1 && sudo systemctl restart gateway.service"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

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
    ):
        """
        Initialize the command server.

        Args:
            port: TCP port to listen on
            command_queue: CommandQueue for reliable command delivery
            discovery_config: Config for node discovery (retries, backoff params)
        """
        super().__init__(daemon=True, name="CommandServer")
        self.port = port
        self.command_queue = command_queue
        self.discovery_config = discovery_config or {}
        self.transceiver = None  # Set later via set_transceiver()
        self._server: HTTPServer | None = None

        # Set later via set_gateway_state()
        self.gateway_state = None
        self.gateway_params = None

    def set_transceiver(self, transceiver) -> None:
        """Set the transceiver reference for discovery support."""
        self.transceiver = transceiver
        if self._server:
            self._server.transceiver = transceiver  # type: ignore

    def set_gateway_state(self, gateway_state) -> None:
        """
        Set up gateway state and parameter registry.

        Call after radio and command_queue are initialized.
        Uses RadioState for staged radio config.
        """
        from gateway.params import GatewayParamRegistry, build_gateway_params

        self.gateway_state = gateway_state
        params = build_gateway_params(gateway_state)
        self.gateway_params = GatewayParamRegistry(params, gateway_state.config_path)

        if self._server:
            self._server.gateway_state = gateway_state  # type: ignore
            self._server.gateway_params = self.gateway_params  # type: ignore

    def run(self) -> None:
        """Run the HTTP server (called by Thread.start())."""
        self._server = HTTPServer(("0.0.0.0", self.port), CommandHandler)
        self._server.command_queue = self.command_queue  # type: ignore
        self._server.discovery_config = self.discovery_config  # type: ignore
        self._server.transceiver = self.transceiver  # type: ignore
        self._server.gateway_state = getattr(self, "gateway_state", None)  # type: ignore
        self._server.gateway_params = self.gateway_params  # type: ignore
        self._server.config_path = getattr(self.gateway_state, "config_path", "") if self.gateway_state else ""  # type: ignore
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
