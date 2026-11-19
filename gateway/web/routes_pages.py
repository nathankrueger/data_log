"""HTML pages and static assets."""

from __future__ import annotations

from gateway.web.core import Router
from gateway.web.static_files import StaticFiles


def register(router: Router, static: StaticFiles) -> None:
    router.add("GET", "/", lambda req: static.response("index.html", req))
    router.add("GET", "/logger", lambda req: static.response("logger.html", req))
    router.add("GET", "/static/{path:path}", lambda req: static.response(req.params["path"], req))
    # Browsers request this on their own; without an explicit route it would
    # fall through to the GET /{cmd} catch-all and broadcast a LoRa command.
    router.add("GET", "/favicon.ico", lambda req: static.response("img/favicon.jpg", req))
