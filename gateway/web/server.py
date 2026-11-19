"""
Gateway web server: LoRa command API + browser UI.

One threaded stdlib HTTP server hosts both:
  - the command API used by the Pi 5 dashboard and scripts/*.sh
    (POST /command, GET /{cmd}/{node}, /discover, /gateway/*), and
  - the browser UI (static/ pages plus JSON endpoints under /api/).

Why not gunicorn/uwsgi: the server must live *inside* the gateway process.
It shares the command queue, radio state and sensor collector in memory, and
the radio can only be owned by one process. Pre-fork servers want to own the
process (signals, worker lifecycle) and would split that state. A
thread-per-connection server is also what the long-blocking command endpoints
need (/discover can wait for a minute) - a fixed worker pool would starve.

Route modules (registration order matters - the command catch-alls go last):
  routes_pages.py      /  /logger  /static/*
  routes_live.py       /api/status  /api/params  /api/readings  /api/feed  /api/nodes/order
  routes_logger.py     /api/logger/*
  routes_commands.py   /command  /discover  /gateway/*  /{cmd}  /{cmd}/{node}
"""

from __future__ import annotations

import gzip
import logging
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from gateway.web.core import (
    HTTPError,
    Request,
    Response,
    Router,
    StreamResponse,
    json_response,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 1 << 20
GZIP_MIN_BYTES = 1400
# Concurrent connections (= handler threads). Browsers hold ~6 keep-alive
# connections per tab; long command waits hold one each. Past the cap new
# connections are closed instead of spawning threads without bound.
MAX_CONNECTIONS = 96
BIND_RETRY_MAX_S = 60.0
# Socket timeout for every read/write on a connection: bounds idle keep-alive
# connections (each holds a slot and a thread) and stalled clients.
REQUEST_TIMEOUT_S = 15.0
_BUSY_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
    b"Content-Length: 29\r\nRetry-After: 2\r\nConnection: close\r\n\r\n"
    b'{"error":"server busy, retry"}'
)
_GZIP_TYPES = ("application/json", "text/")


class WebContext:
    """
    References the route handlers need. Most are filled in after the server
    starts (the radio comes up after the HTTP server), so handlers must read
    them at request time and tolerate None.
    """

    def __init__(self, command_queue, discovery_config: dict | None = None, config_path: str = ""):
        self.command_queue = command_queue
        self.discovery_config = discovery_config or {}
        self.config_path = config_path
        self.transceiver = None
        self.gateway_state = None
        self.gateway_params = None
        self.collector = None
        self.logger_service = None
        self.node_order: list[str] = []  # UI card order, persisted as web.node_order
        self.hostname = socket.gethostname()


def build_router(ctx: WebContext) -> Router:
    from gateway.web import routes_commands, routes_live, routes_logger, routes_pages
    from gateway.web.static_files import StaticFiles

    router = Router()
    routes_pages.register(router, StaticFiles(STATIC_DIR))
    routes_live.register(router, ctx)
    routes_logger.register(router, ctx)
    routes_commands.register(router, ctx)  # last: contains /{cmd} catch-alls
    return router


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64
    router: Router

    def __init__(
        self,
        address,
        handler,
        max_connections: int = MAX_CONNECTIONS,
        request_timeout: float = REQUEST_TIMEOUT_S,
    ):
        self._slots = threading.BoundedSemaphore(max_connections)
        self.request_timeout = request_timeout
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            # Over capacity: answer 503 without reading the request or
            # spawning a thread, so clients (e.g. the Pi 5 dashboard) see a
            # retryable error instead of a dead connection.
            logger.warning(f"Connection limit reached; refusing {client_address[0]} with 503")
            try:
                request.settimeout(1.0)
                request.sendall(_BUSY_RESPONSE)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)  # starts the handler thread
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address) -> None:
        """Errors that escaped a handler (route errors are already caught)."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            logger.debug(f"Connection from {client_address[0]} ended: {exc!r}")
        else:
            logger.warning(f"Error handling connection from {client_address[0]}: {exc!r}")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: snappier polling from phones
    server_version = "DataLogGateway/2"
    sys_version = ""

    @property
    def timeout(self) -> float:
        """Applied to the socket by StreamRequestHandler.setup(): bounds idle
        keep-alive connections and dead clients."""
        return self.server.request_timeout
    # Headers and body go out as separate writes; without this, Nagle +
    # delayed ACK can stall each keep-alive poll by tens of milliseconds.
    disable_nagle_algorithm = True

    server: _Server

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    # -- dispatch -----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        split = urlsplit(self.path)
        req: Request | None = None
        try:
            body = self._read_body()
            req = Request(
                method,
                split.path,
                parse_qs(split.query, keep_blank_values=True),
                self.headers,
                body,
            )
            handler, req.params = self.server.router.resolve(method, split.path)
            resp = handler(req)
        except HTTPError as e:
            payload = {"error": e.message}
            payload.update(e.extra)
            resp = json_response(payload, status=e.status)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        except Exception as e:  # noqa: BLE001 - last-resort guard, keep serving
            logger.exception("Unhandled error serving %s %s", method, split.path)
            resp = json_response({"error": "internal", "message": str(e)}, status=500)

        try:
            if isinstance(resp, StreamResponse):
                self._send_stream(resp)
            else:
                self._send(resp, req)
        except OSError:
            # Client went away mid-response
            self.close_connection = True

    def _read_body(self) -> bytes:
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            self.close_connection = True
            raise HTTPError(411, "Chunked request bodies are not supported")
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            self.close_connection = True
            raise HTTPError(400, "Invalid Content-Length") from None
        if n < 0 or n > MAX_BODY_BYTES:
            self.close_connection = True
            raise HTTPError(413, "Request body too large")
        if not n:
            return b""
        try:
            body = self.rfile.read(n)
        except OSError:  # incl. socket timeout: framing is lost, so close
            self.close_connection = True
            raise HTTPError(408, "Timed out reading the request body") from None
        if len(body) < n:
            self.close_connection = True
            raise HTTPError(400, "Incomplete request body")
        return body

    # -- responses ----------------------------------------------------------

    def _send(self, resp: Response, req: Request | None) -> None:
        body = resp.body
        headers = dict(resp.headers)
        if (
            len(body) >= GZIP_MIN_BYTES
            and "Content-Encoding" not in headers
            and resp.content_type.startswith(_GZIP_TYPES)
            and req is not None
            and req.accepts_gzip()
        ):
            body = gzip.compress(body, compresslevel=resp.gzip_level)
            headers["Content-Encoding"] = "gzip"
            headers.setdefault("Vary", "Accept-Encoding")

        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        if resp.status not in (204, 304):
            self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body and resp.status not in (204, 304):
            self.wfile.write(body)

    def _send_stream(self, resp: StreamResponse) -> None:
        # HTTP/1.0 clients don't understand chunked encoding: send the raw
        # body and mark the end by closing the connection instead.
        chunked = self.request_version == "HTTP/1.1"
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        for key, value in resp.headers.items():
            self.send_header(key, value)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Connection", "close")
        self.end_headers()

        chunks = iter(resp.chunks)
        try:
            for chunk in chunks:
                if chunk:
                    if chunked:
                        self.wfile.write(b"%x\r\n%b\r\n" % (len(chunk), chunk))
                    else:
                        self.wfile.write(chunk)
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except OSError:
            self.close_connection = True
        except Exception:  # noqa: BLE001
            # Headers are already sent; dropping the connection is the only
            # way left to tell the client the body is incomplete.
            logger.exception("Error while streaming %s", self.path)
            self.close_connection = True
        finally:
            close = getattr(chunks, "close", None)
            if close is not None:
                close()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        """Silence per-request access logging (the gateway logs what matters)."""
        pass


class WebServer(threading.Thread):
    """
    HTTP server thread for the command API and browser UI.

    Drop-in replacement for the old CommandServer: same constructor and
    set_* wiring methods, plus set_logger_service().
    """

    def __init__(
        self,
        port: int,
        command_queue,
        discovery_config: dict | None = None,
        config_path: str = "",
        host: str = "0.0.0.0",
        max_connections: int = MAX_CONNECTIONS,
        request_timeout: float = REQUEST_TIMEOUT_S,
    ):
        super().__init__(daemon=True, name="WebServer")
        self.port = port
        self.host = host
        self.max_connections = max_connections
        self.request_timeout = request_timeout
        self.context = WebContext(command_queue, discovery_config, config_path)
        self.router = build_router(self.context)
        self._server: _Server | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self.bound_port: int | None = None

    # -- wiring (may be called before or after start) ------------------------

    def set_transceiver(self, transceiver) -> None:
        self.context.transceiver = transceiver

    def set_sensor_collector(self, collector) -> None:
        self.context.collector = collector

    def set_logger_service(self, service) -> None:
        self.context.logger_service = service

    def set_node_order(self, ids) -> None:
        if isinstance(ids, list):
            self.context.node_order = [i for i in ids if isinstance(i, str)]

    def set_gateway_state(self, gateway_state) -> None:
        """Attach gateway state and (re)build the parameter registry from it."""
        from gateway.params import GatewayParamRegistry, build_gateway_params

        self.context.gateway_state = gateway_state
        if not self.context.config_path:
            self.context.config_path = gateway_state.config_path
        params = build_gateway_params(gateway_state)
        self.context.gateway_params = GatewayParamRegistry(params, gateway_state.config_path)

    # -- lifecycle ----------------------------------------------------------

    def run(self) -> None:
        """
        Serve until stop(). Never gives up: a busy port at boot (e.g. the old
        process still in TIME_WAIT) is retried with backoff, and an unexpected
        error in the accept loop rebuilds the server, so the command API and UI
        can't silently disappear for the rest of the gateway's uptime.
        """
        delay = 1.0
        while not self._stopping.is_set():
            try:
                server = _Server(
                    (self.host, self.port), _Handler, self.max_connections, self.request_timeout
                )
            except OSError as e:
                logger.error(f"Web server could not bind port {self.port}: {e} (retrying in {delay:.0f}s)")
                if self._stopping.wait(delay):
                    return
                delay = min(delay * 2, BIND_RETRY_MAX_S)
                continue
            delay = 1.0
            server.router = self.router
            self.bound_port = server.server_address[1]
            # Publish and serve with nothing in between: stop() relies on
            # serve_forever() running (or about to run) once _server is set.
            self._server = server
            logger.info(f"Web server listening on http://{self.host}:{self.bound_port}/")
            self._ready.set()
            try:
                server.serve_forever(poll_interval=0.5)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Web server loop failed: {e!r} (restarting)")
            finally:
                self._ready.clear()
                self._server = None
                server.server_close()
            if not self._stopping.is_set():
                self._stopping.wait(1.0)

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Block until the server is accepting connections."""
        return self._ready.wait(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        if threading.current_thread() is self:
            return
        logger.info("Stopping web server")
        # Loop: if run() publishes a server just after we looked, the next
        # pass shuts that one down too (shutdown() returns once serve_forever
        # has exited, and run() sees _stopping before serving again).
        deadline = time.monotonic() + timeout
        while self.is_alive() and time.monotonic() < deadline:
            server = self._server
            if server is not None:
                server.shutdown()
            self.join(0.1)
