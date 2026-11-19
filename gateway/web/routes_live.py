"""
Live-view API for the browser UI.

  GET /api/status              one poll for the whole Live page: identity,
                               uptime, radio params, nodes, feed + logger summary
  GET /api/readings?after=SEQ  feed entries newer than SEQ (O(new))
  PUT /api/feed                {"size": N} - resize the feed, persisted to
                               gateway_config.json as web.feed_size
  PUT /api/nodes/order         {"ids": [...]} - node card order chosen by
                               dragging, persisted as web.node_order
  GET /api/params              params with UI metadata (legacy shape)
"""

from __future__ import annotations

import logging
import time

from gateway.web.core import HTTPError, Request, Router, json_response
from utils.config_persistence import update_config_file

logger = logging.getLogger(__name__)


def register(router: Router, ctx) -> None:
    @router.route("GET", "/api/status")
    def get_status(req: Request):
        now = time.time()
        gs = ctx.gateway_state
        collector = ctx.collector
        svc = ctx.logger_service
        return json_response({
            "now": now,
            "hostname": ctx.hostname,
            "node_id": gs.node_id if gs else "",
            "uptime_seconds": (now - gs.start_time) if gs else None,
            "dashboard_url": gs.dashboard_url if gs else "",
            "lora": ctx.transceiver is not None,
            "params": ctx.gateway_params.get_all_with_meta() if ctx.gateway_params else None,
            "nodes": collector.get_nodes(now) if collector else [],
            "node_order": ctx.node_order,
            "feed": collector.feed_info() if collector else None,
            "logger": svc.summary() if svc else None,
        })

    @router.route("GET", "/api/params")
    def get_params_meta(req: Request):
        registry = ctx.gateway_params
        if registry is None:
            raise HTTPError(503, "unavailable", message="Gateway parameter registry not initialized")
        gs = ctx.gateway_state
        return json_response({
            "params": registry.get_all_with_meta(),
            "uptime_seconds": (time.time() - gs.start_time) if gs else 0,
            "node_id": gs.node_id if gs else "",
        })

    @router.route("GET", "/api/readings")
    def get_readings(req: Request):
        collector = ctx.collector
        if collector is None:
            raise HTTPError(503, "unavailable", message="Sensor collector not initialized")
        after = req.int_arg("after", 0, lo=0)
        epoch = req.int_arg("epoch")
        return json_response(collector.get_feed(after=after, epoch=epoch))

    @router.route("PUT", "/api/feed")
    def put_feed(req: Request):
        collector = ctx.collector
        if collector is None:
            raise HTTPError(503, "unavailable", message="Sensor collector not initialized")
        data = req.json_object()
        size = data.get("size")
        if not isinstance(size, int) or isinstance(size, bool):
            raise HTTPError(400, "'size' must be an integer")
        applied = collector.set_feed_size(size)
        persisted = False
        if ctx.config_path:
            persisted = update_config_file(ctx.config_path, {"web.feed_size": applied})
        logger.info(f"Feed size set to {applied}{'' if persisted else ' (not persisted)'}")
        return json_response({"size": applied, "persisted": persisted})

    @router.route("PUT", "/api/nodes/order")
    def put_node_order(req: Request):
        ids = req.json_object().get("ids")
        if (
            not isinstance(ids, list)
            or len(ids) > 256
            or not all(isinstance(i, str) and 0 < len(i) <= 128 for i in ids)
        ):
            raise HTTPError(400, "'ids' must be a list of node ids")
        ids = list(dict.fromkeys(ids))  # de-duplicate, keep order
        ctx.node_order = ids
        persisted = False
        if ctx.config_path:
            persisted = update_config_file(ctx.config_path, {"web.node_order": ids})
        return json_response({"ids": ids, "persisted": persisted})
