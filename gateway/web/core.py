"""
Tiny HTTP toolkit for the gateway web server: Request, Response, Router.

Deliberately small. The gateway serves a handful of LAN clients, so a web
framework would add memory and dependencies without buying anything. Route
handlers take a Request and return a Response (or StreamResponse); raising
HTTPError produces a JSON error body with the given status.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable, Iterable
from urllib.parse import unquote

JSON_TYPE = "application/json"


class HTTPError(Exception):
    """Raise from a route handler to return a JSON error response."""

    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class Request:
    """Parsed HTTP request handed to route handlers."""

    __slots__ = ("method", "path", "query", "headers", "body", "params")

    def __init__(self, method: str, path: str, query: dict[str, list[str]], headers, body: bytes):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.params: dict[str, str] = {}

    # -- body ---------------------------------------------------------------

    def json(self) -> Any:
        """Decode the body as JSON (400 on empty or malformed bodies)."""
        if not self.body:
            raise HTTPError(400, "Empty request body")
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPError(400, f"Invalid JSON: {e}") from None

    def json_object(self) -> dict:
        """Decode the body as a JSON object (400 if it is anything else)."""
        data = self.json()
        if not isinstance(data, dict):
            raise HTTPError(400, "Expected a JSON object")
        return data

    # -- query string -------------------------------------------------------

    def arg(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def arg_list(self, name: str) -> list[str]:
        return [v for v in self.query.get(name, []) if v != ""]

    def int_arg(
        self, name: str, default: int | None = None, lo: int | None = None, hi: int | None = None
    ) -> int | None:
        raw = self.arg(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError:
            raise HTTPError(400, f"'{name}' must be an integer") from None
        return _clamp(value, lo, hi)

    def float_arg(
        self, name: str, default: float | None = None, lo: float | None = None, hi: float | None = None
    ) -> float | None:
        raw = self.arg(name)
        if raw is None or raw == "":
            return default
        try:
            value = float(raw)
        except ValueError:
            raise HTTPError(400, f"'{name}' must be a number") from None
        if not math.isfinite(value):
            raise HTTPError(400, f"'{name}' must be finite")
        return _clamp(value, lo, hi)

    def accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "")


def _clamp(value, lo, hi):
    if lo is not None and value < lo:
        return lo
    if hi is not None and value > hi:
        return hi
    return value


class Response:
    """A complete in-memory response."""

    __slots__ = ("status", "body", "content_type", "headers", "gzip_level")

    def __init__(
        self,
        body: bytes = b"",
        status: int = 200,
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
        gzip_level: int = 1,
    ):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}
        # Dynamic responses use a cheap level; static files arrive pre-compressed.
        self.gzip_level = gzip_level


class StreamResponse:
    """A response streamed with chunked transfer encoding (e.g. CSV export)."""

    __slots__ = ("status", "chunks", "content_type", "headers")

    def __init__(
        self,
        chunks: Iterable[bytes],
        status: int = 200,
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self.chunks = chunks
        self.content_type = content_type
        self.headers = headers or {}


def _sanitize(obj: Any) -> Any:
    """Replace non-finite floats (NaN/inf) with None so browsers can JSON.parse it."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def dumps(obj: Any, compact: bool = True) -> bytes:
    """
    Serialize to JSON bytes.

    compact=False keeps json.dumps' default ", " / ": " separators, which the
    command API has always used (scripts and the Pi 5 dashboard see the same
    bytes as before).
    """
    separators = (",", ":") if compact else None
    try:
        text = json.dumps(obj, separators=separators, allow_nan=False)
    except ValueError:
        text = json.dumps(_sanitize(obj), separators=separators, allow_nan=False)
    return text.encode("utf-8")


def json_response(
    obj: Any, status: int = 200, headers: dict[str, str] | None = None, compact: bool = True
) -> Response:
    hdrs = {"Cache-Control": "no-store"}
    if headers:
        hdrs.update(headers)
    return Response(dumps(obj, compact=compact), status, JSON_TYPE, hdrs)


Handler = Callable[[Request], "Response | StreamResponse"]


class Router:
    """
    First-match-wins router.

    Patterns are literal segments plus ``{name}`` (one segment) and
    ``{name:path}`` (the remainder, at least one segment). Registration order
    matters: put catch-all patterns last.
    """

    def __init__(self) -> None:
        self._routes: list[tuple[str, tuple[tuple[str, str], ...], Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        self._routes.append((method.upper(), _compile(pattern), handler))

    def route(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        def decorator(fn: Handler) -> Handler:
            self.add(method, pattern, fn)
            return fn

        return decorator

    def resolve(self, method: str, path: str) -> tuple[Handler, dict[str, str]]:
        segments = _split(path, decode=True)
        for route_method, pattern, handler in self._routes:
            if route_method != method:
                continue
            params = _match(pattern, segments)
            if params is not None:
                return handler, params
        raise HTTPError(404, "Not Found")


def _split(path: str, decode: bool) -> list[str]:
    path = path.strip("/")
    if not path:
        return []
    parts = path.split("/")
    return [unquote(p) for p in parts] if decode else parts


def _compile(pattern: str) -> tuple[tuple[str, str], ...]:
    compiled = []
    for seg in _split(pattern, decode=False):
        if seg.startswith("{") and seg.endswith("}"):
            name = seg[1:-1]
            if name.endswith(":path"):
                compiled.append(("*", name[: -len(":path")]))
            else:
                compiled.append(("=", name))
        else:
            compiled.append(("", seg))
    return tuple(compiled)


def _match(pattern: tuple[tuple[str, str], ...], segments: list[str]) -> dict[str, str] | None:
    params: dict[str, str] = {}
    for i, (kind, value) in enumerate(pattern):
        if kind == "*":
            rest = segments[i:]
            if not rest:
                return None
            params[value] = "/".join(rest)
            return params
        if i >= len(segments):
            return None
        seg = segments[i]
        if kind == "":
            if seg != value:
                return None
        else:
            if not seg:
                return None
            params[value] = seg
    return params if len(segments) == len(pattern) else None
