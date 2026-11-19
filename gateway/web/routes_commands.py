"""
LoRa command API (gateway -> node) and gateway parameter endpoints.

Ported from the original gateway/http_handler.py with identical paths, status
codes and JSON bodies (non-compact separators preserved). Differences:
  - error bodies are JSON ({"error": ...}) instead of stdlib HTML error pages
  - malformed numeric query args return 400 instead of dropping the connection
  - the /{cmd} catch-alls refuse reserved names and names containing '.', so
    browser requests like /favicon.ico or /apple-touch-icon.png can never be
    broadcast over LoRa

Endpoints:
  POST /command                      queue a command (fire and forget)
  GET  /discover[?retries=N]         discover reachable nodes
  GET  /{cmd}?expected_acks=N&a=X    broadcast, wait for N ACKs
  GET  /{cmd}/{node_id}?a=X          send to one node, wait for its response
  GET  /gateway/uptime
  GET  /gateway/params               all gateway params
  GET  /gateway/param/{name}
  PUT  /gateway/param/{name}         body {"value": X}
  POST /gateway/rcfg_radio           apply staged radio params
  POST /gateway/savecfg              persist params to the config file
  POST /gateway/flush_commands
  POST /gateway/restart
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time

from gateway.web.core import HTTPError, Request, Router, json_response
from utils.config_persistence import update_config_file
from utils.radio_state import BW_HZ_MAP

logger = logging.getLogger("gateway.http_handler")
cmd_logger = logging.getLogger("cmd_debug")

# Retry count for fire-and-forget commands (no_wait=1)
# Used for rcfg_radio where ACK is unreliable after radio params change
NO_WAIT_MAX_RETRIES = 2

# First path segments that belong to the gateway itself, never to node commands
RESERVED_SEGMENTS = frozenset({"api", "static", "gateway", "logger", "discover", "command"})


def _reply(obj, status: int = 200):
    """JSON response using the command API's historical (non-compact) format."""
    return json_response(obj, status=status, compact=False)


def register(router: Router, ctx) -> None:
    def queue():
        return ctx.command_queue

    def registry():
        reg = ctx.gateway_params
        if reg is None:
            raise HTTPError(503, "unavailable", message="Gateway parameter registry not initialized")
        return reg

    # -- POST /command ------------------------------------------------------

    @router.route("POST", "/command")
    def post_command(req: Request):
        data = req.json_object()

        cmd = data.get("cmd")
        if not cmd or not isinstance(cmd, str):
            raise HTTPError(400, "Missing or invalid 'cmd' field")

        args = data.get("args", [])
        if not isinstance(args, list):
            raise HTTPError(400, "'args' must be a list")
        args = [str(arg) for arg in args]

        node_id = data.get("node_id", "")
        if not isinstance(node_id, str):
            raise HTTPError(400, "'node_id' must be a string")

        expected_acks = data.get("expected_acks", 1)
        if not isinstance(expected_acks, int) or isinstance(expected_acks, bool) or expected_acks < 1:
            raise HTTPError(400, "'expected_acks' must be a positive integer")

        command_id = queue().add(cmd, args, node_id, expected_acks=expected_acks)
        if command_id is None:
            raise HTTPError(503, "Command queue full")

        target = node_id if node_id else "broadcast"
        logger.info(
            f"Queued command '{cmd}' for {target} "
            f"(id: {command_id}, expected_acks: {expected_acks})"
        )
        return _reply({
            "status": "queued",
            "command_id": command_id,
            "cmd": cmd,
            "target": target,
            "expected_acks": expected_acks,
        })

    # -- gateway control ----------------------------------------------------

    @router.route("POST", "/gateway/restart")
    def post_restart(req: Request):
        # Detached shell sleeps 1s so this response is delivered before we die.
        logger.info("Restart endpoint triggered - spawning systemctl restart in 1s")
        subprocess.Popen(
            ["sh", "-c", "sleep 1 && sudo systemctl restart gateway.service"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return _reply({"status": "restarting"})

    @router.route("POST", "/gateway/rcfg_radio")
    def post_rcfg_radio(req: Request):
        """Apply staged radio config (no persist). The transceiver thread does
        the SPI work; we wait for it to pick up the pending change."""
        gs = ctx.gateway_state
        if gs is None or gs.radio_state is None:
            raise HTTPError(503, "unavailable", message="Radio state not initialized")
        radio_state = gs.radio_state

        if not radio_state.has_pending():
            return _reply({"r": "nothing"})

        success, applied = radio_state.wait_for_apply(timeout=1.0)
        if not success:
            return _reply({
                "error": "timeout",
                "message": "Timed out waiting for config to be applied",
            }, 504)

        logger.info(f"Gateway rcfg_radio applied: {', '.join(applied)}")
        return _reply({"r": ", ".join(applied)})

    @router.route("POST", "/gateway/savecfg")
    def post_savecfg(req: Request):
        """Persist ALL current writable params to the config file."""
        gs = ctx.gateway_state
        reg = ctx.gateway_params
        if gs is None or reg is None:
            raise HTTPError(503, "unavailable", message="Gateway state not initialized")
        config_path = gs.config_path
        if not config_path:
            raise HTTPError(503, "unavailable", message="No config path configured")

        updates = {}
        for name, p in reg._params.items():
            if p.config_key and p.setter is not None:
                val = p.getter()
                if name == "bw":
                    val = BW_HZ_MAP.get(val, val)  # store Hz, not code
                updates[p.config_key] = val

        if not updates:
            return _reply({"r": "unchanged"})

        update_config_file(config_path, updates)
        logger.info(f"Gateway savecfg: persisted {len(updates)} params")
        return _reply({"r": "saved"})

    @router.route("POST", "/gateway/flush_commands")
    def post_flush_commands(req: Request):
        count = queue().flush()
        return _reply({"flushed": count})

    @router.route("GET", "/gateway/uptime")
    def get_uptime(req: Request):
        gs = ctx.gateway_state
        if gs is None:
            raise HTTPError(503, "unavailable", message="Gateway state not initialized")
        return _reply({"uptime_seconds": time.time() - gs.start_time})

    @router.route("GET", "/gateway/params")
    def get_params(req: Request):
        return _reply(registry().get_all())

    @router.route("GET", "/gateway/param/{name}")
    def get_param(req: Request):
        name = req.params["name"]
        value, error = registry().get(name)
        if error:
            return _reply({"error": error}, 400)
        return _reply({name: value})

    @router.route("PUT", "/gateway/param/{name}")
    def put_param(req: Request):
        name = req.params["name"]
        data = req.json_object()
        if "value" not in data:
            raise HTTPError(400, "Missing 'value' field in JSON body")
        new_value, error = registry().set(name, str(data["value"]))
        if error:
            return _reply({"error": error}, 400)
        logger.info(f"Gateway param '{name}' set to {new_value}")
        return _reply({name: new_value})

    # -- discovery ----------------------------------------------------------

    @router.route("GET", "/discover")
    def get_discover(req: Request):
        transceiver = ctx.transceiver
        if transceiver is None:
            raise HTTPError(503, "unavailable", message="LoRa transceiver not running")

        disc = ctx.discovery_config
        # Capped: discovery pauses all other LoRa commands while it runs
        retries = req.int_arg("retries", disc.get("discovery_retries", 30), lo=1, hi=100)

        from gateway.command_queue import DiscoveryRequest

        request = DiscoveryRequest(
            retries=retries,
            initial_retry_ms=disc.get("initial_retry_ms", 500),
            max_retry_ms=disc.get("max_retry_ms", 5000),
            retry_multiplier=disc.get("retry_multiplier", 1.5),
            done=threading.Event(),
        )
        if not transceiver.request_discovery(request):
            return _reply({"error": "conflict", "message": "Discovery already in progress"}, 409)

        expected_ms = 0.0
        delay = float(request.initial_retry_ms)
        for _ in range(retries):
            expected_ms += delay
            delay = min(delay * request.retry_multiplier, float(request.max_retry_ms))
        wait_timeout = (expected_ms / 1000.0) + 10.0

        logger.info(
            f"Discovery requested ({retries} broadcasts, "
            f"timeout={wait_timeout:.0f}s), waiting for completion..."
        )
        if not request.done.wait(timeout=wait_timeout):
            return _reply({"error": "timeout", "message": "Discovery did not complete in time"}, 504)
        if request.error:
            return _reply({"error": "discovery_error", "message": request.error}, 500)
        return _reply({"nodes": request.nodes, "count": len(request.nodes)})

    # -- catch-alls: node commands (must be registered last) -----------------

    def check_cmd(cmd: str) -> None:
        if cmd in RESERVED_SEGMENTS or "." in cmd:
            raise HTTPError(404, "Not Found")

    @router.route("GET", "/{cmd}")
    def get_broadcast(req: Request):
        cmd = req.params["cmd"]
        check_cmd(cmd)
        expected_acks = req.int_arg("expected_acks", 1, lo=1)
        args = req.arg_list("a")  # blank values dropped, as before

        q = queue()
        command_id = q.add(cmd, args, node_id="", expected_acks=expected_acks)
        if command_id is None:
            raise HTTPError(503, "Command queue full")

        logger.info(f"Queued broadcast '{cmd}' (expected_acks={expected_acks}), waiting for response...")
        wait_timeout = q.wait_timeout
        response = q.wait_for_response(command_id, timeout=wait_timeout)
        if response is not None:
            return _reply(response)

        partial = q.get_partial_acks(command_id)
        q.cancel(command_id)
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
        return _reply(result, 504)

    @router.route("GET", "/{cmd}/{node_id}")
    def get_unicast(req: Request):
        cmd = req.params["cmd"]
        node_id = req.params["node_id"]
        check_cmd(cmd)
        args = req.arg_list("a")  # blank values dropped, as before
        q = queue()

        no_wait = req.arg("no_wait", "0") == "1"
        cmd_logger.debug("HTTP_GET cmd=%s node=%s query=%s no_wait=%s", cmd, node_id, req.query, no_wait)

        if no_wait:
            # Fire-and-forget with reduced retries (rcfg_radio: ACK unreliable
            # once the node has switched radio params)
            cmd_logger.debug(
                "QUEUE_ADD cmd=%s node=%s max_retries=%d (no_wait)", cmd, node_id, NO_WAIT_MAX_RETRIES
            )
            command_id = q.add(cmd, args, node_id, max_retries=NO_WAIT_MAX_RETRIES)
            if command_id is None:
                raise HTTPError(503, "Command queue full")
            logger.info(f"Queued '{cmd}' for {node_id} (no_wait mode)")
            return _reply({"status": "queued", "id": command_id}, 202)

        command_id = q.add(cmd, args, node_id)
        if command_id is None:
            raise HTTPError(503, "Command queue full")

        logger.info(f"Queued '{cmd}' for {node_id}, waiting for response...")
        wait_timeout = q.wait_timeout
        response = q.wait_for_response(command_id, timeout=wait_timeout)
        if response is not None:
            # Non-empty dict = payload from node; empty dict = ACK with no payload
            return _reply(response if response else {"status": "acked"})

        # Cancel so it doesn't block subsequent commands
        q.cancel(command_id)
        return _reply({
            "error": "timeout",
            "message": f"No response from node '{node_id}' within {wait_timeout} seconds",
        }, 504)
