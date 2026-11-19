"""
History logger API for the browser UI.

  GET    /api/logger                     state: switch, charts, series catalog, storage
  PUT    /api/logger                     {"enabled": bool}
  POST   /api/logger/charts              create chart {title, series: [ids], max_age_s}
  PUT    /api/logger/charts/{id}         update chart (same fields)
  DELETE /api/logger/charts/{id}
  PUT    /api/logger/charts/{id}/options {"auto_range": bool} view options
  PUT    /api/logger/order               {"ids": [...], "new_rows": [...]} chart order;
                                         new_rows (optional) = charts starting a grid row
  GET    /api/logger/data                s=ID (repeat) start end points [tier since auto]
  GET    /api/logger/export.csv          s=ID (repeat; omit for all) start end
                                         tz (IANA) tzo (minutes) name layout=wide|long
  GET    /api/logger/storage             per-series storage details
  POST   /api/logger/purge               {"series": [ids]} or {"all": true}
"""

from __future__ import annotations

import time

from gateway.web.core import HTTPError, Request, Router, StreamResponse, json_response

# Input bounds: keep every request's cost predictable. One chart's worth of
# series per data/export request (export-all omits s= and streams instead).
from gateway.timeseries.constants import MAX_SERIES_PER_CHART

MAX_QUERY_SERIES = MAX_SERIES_PER_CHART
TIME_MIN_S = 0.0
TIME_MAX_S = 1e10  # year 2286; also keeps ms timestamps inside SQLite's int64


def register(router: Router, ctx) -> None:
    def service():
        svc = ctx.logger_service
        if svc is None:
            raise HTTPError(503, "unavailable", message="History logger not configured")
        return svc

    def call(fn, *args):
        from gateway.timeseries.service import NotFound

        try:
            return fn(*args)
        except NotFound as e:
            raise HTTPError(404, str(e)) from None
        except ValueError as e:
            raise HTTPError(400, str(e)) from None

    @router.route("GET", "/api/logger")
    def get_state(req: Request):
        return json_response(service().state())

    @router.route("PUT", "/api/logger")
    def put_state(req: Request):
        data = req.json_object()
        if not isinstance(data.get("enabled"), bool):
            raise HTTPError(400, "'enabled' must be true or false")
        svc = service()
        call(svc.set_enabled, data["enabled"])
        return json_response(svc.state())

    @router.route("POST", "/api/logger/charts")
    def post_chart(req: Request):
        chart = call(service().create_chart, req.json_object())
        return json_response({"chart": chart}, status=201)

    @router.route("PUT", "/api/logger/charts/{id}")
    def put_chart(req: Request):
        chart = call(service().update_chart, req.params["id"], req.json_object())
        return json_response({"chart": chart})

    @router.route("PUT", "/api/logger/charts/{id}/options")
    def put_chart_options(req: Request):
        chart = call(service().set_chart_options, req.params["id"], req.json_object())
        return json_response({"chart": chart})

    @router.route("DELETE", "/api/logger/charts/{id}")
    def delete_chart(req: Request):
        call(service().delete_chart, req.params["id"])
        return json_response({"deleted": req.params["id"]})

    @router.route("PUT", "/api/logger/order")
    def put_order(req: Request):
        data = req.json_object()
        ids = data.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise HTTPError(400, "'ids' must be a list of chart ids")
        new_rows = data.get("new_rows")
        if new_rows is not None and (
            not isinstance(new_rows, list) or not all(isinstance(i, str) for i in new_rows)
        ):
            raise HTTPError(400, "'new_rows' must be a list of chart ids")
        service().reorder(ids, new_rows)
        return json_response({"ok": True})

    def window(req: Request) -> tuple[float, float]:
        now = time.time()
        end = req.float_arg("end", now, lo=TIME_MIN_S, hi=TIME_MAX_S)
        start = req.float_arg("start", end - 3600.0, lo=TIME_MIN_S, hi=TIME_MAX_S)
        if start >= end:
            raise HTTPError(400, "'start' must be before 'end'")
        return start, end

    def series_keys(req: Request) -> list[str]:
        keys = list(dict.fromkeys(req.arg_list("s")))
        if len(keys) > MAX_QUERY_SERIES:
            raise HTTPError(400, f"at most {MAX_QUERY_SERIES} series per request")
        return keys

    @router.route("GET", "/api/logger/data")
    def get_data(req: Request):
        keys = series_keys(req)
        if not keys:
            raise HTTPError(400, "at least one series (s=...) is required")
        start, end = window(req)
        result = call(
            service().query,
            keys,
            start,
            end,
            req.int_arg("points", 800),
            req.int_arg("tier"),
            req.float_arg("since", lo=TIME_MIN_S, hi=TIME_MAX_S),
            req.arg("auto") in ("1", "true"),
        )
        return json_response(result)

    @router.route("GET", "/api/logger/export.csv")
    def get_export(req: Request):
        keys = series_keys(req) or None
        start, end = window(req)
        layout = req.arg("layout", "wide")
        if layout not in ("wide", "long"):
            raise HTTPError(400, "'layout' must be wide or long")
        if keys is None:
            layout = "long"  # all series: wide would be mostly blanks

        from gateway.timeseries.export import safe_filename

        chunks = call(
            service().export_csv,
            keys,
            start,
            end,
            req.arg("tz"),
            req.int_arg("tzo", None, lo=-24 * 60, hi=24 * 60),
            layout,
        )
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(end))
        filename = f"{safe_filename(req.arg('name') or 'gateway-history')}_{stamp}.csv"
        return StreamResponse(
            chunks,
            content_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @router.route("GET", "/api/logger/storage")
    def get_storage(req: Request):
        return json_response(service().storage())

    @router.route("POST", "/api/logger/purge")
    def post_purge(req: Request):
        data = req.json_object()
        if data.get("all") is True:
            keys = None
        else:
            keys = data.get("series")
            if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
                raise HTTPError(400, "'series' must be a non-empty list of ids, or pass {\"all\": true}")
        purged = service().purge(keys)
        return json_response({"purged": len(purged)})
