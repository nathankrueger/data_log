"""
Status page loader for the gateway HTTP server.

Reads gateway/status.html from disk and caches it for serving.
"""

from pathlib import Path

_cached_html: str | None = None


def get_status_page_html() -> str:
    """Return the status page HTML, reading from disk on first call."""
    global _cached_html
    if _cached_html is None:
        html_path = Path(__file__).parent / "status.html"
        _cached_html = html_path.read_text(encoding="utf-8")
    return _cached_html
