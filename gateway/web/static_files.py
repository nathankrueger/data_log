"""
Static file serving for the gateway UI.

Files are read on first request and cached in memory together with a gzip
copy, keyed by (mtime, size) so edits on disk are picked up without a restart.
Responses carry an ETag; app assets are served with ``no-cache`` (always
revalidate, which costs a 304) and versioned vendor files as immutable.
"""

from __future__ import annotations

import gzip
import mimetypes
import stat
import threading
from pathlib import Path

from gateway.web.core import HTTPError, Request, Response

_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}
_COMPRESSIBLE = {".html", ".css", ".js", ".mjs", ".json", ".svg", ".txt"}


class _Entry:
    __slots__ = ("mtime", "size", "etag", "ctype", "data", "gz")

    def __init__(self, mtime: int, size: int, ctype: str, data: bytes, gz: bytes | None):
        self.mtime = mtime
        self.size = size
        self.etag = f'"{mtime:x}-{size:x}"'
        self.ctype = ctype
        self.data = data
        self.gz = gz


class StaticFiles:
    """Serve files from a directory with in-memory caching, ETags and gzip."""

    def __init__(self, root: Path):
        self._root = root.resolve()
        self._cache: dict[Path, _Entry] = {}
        self._lock = threading.Lock()

    def response(self, rel_path: str, req: Request) -> Response:
        path = (self._root / rel_path).resolve()
        if path != self._root and self._root not in path.parents:
            raise HTTPError(404, "Not Found")
        try:
            st = path.stat()
        except OSError:
            raise HTTPError(404, "Not Found") from None
        if not stat.S_ISREG(st.st_mode):
            raise HTTPError(404, "Not Found")

        entry = self._get(path, st)
        immutable = rel_path.startswith("vendor/")
        headers = {
            "ETag": entry.etag,
            "Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache",
            "Vary": "Accept-Encoding",
        }

        if entry.etag in (req.headers.get("If-None-Match") or ""):
            return Response(b"", 304, entry.ctype, headers)

        if entry.gz is not None and req.accepts_gzip():
            headers["Content-Encoding"] = "gzip"
            return Response(entry.gz, 200, entry.ctype, headers)
        return Response(entry.data, 200, entry.ctype, headers)

    def _get(self, path: Path, st) -> _Entry:
        with self._lock:
            entry = self._cache.get(path)
            if entry is not None and entry.mtime == st.st_mtime_ns and entry.size == st.st_size:
                return entry

        data = path.read_bytes()
        suffix = path.suffix.lower()
        ctype = _TYPES.get(suffix) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        gz = None
        if suffix in _COMPRESSIBLE and len(data) >= 512:
            gz = gzip.compress(data, compresslevel=9)
            if len(gz) >= len(data):
                gz = None
        entry = _Entry(st.st_mtime_ns, st.st_size, ctype, data, gz)
        with self._lock:
            self._cache[path] = entry
        return entry
