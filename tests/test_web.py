"""Tests for the web server toolkit, live feed state and route wiring."""

import json
import threading
import time
import urllib.request
from urllib.error import HTTPError as UrlHTTPError

import pytest

from gateway.command_queue import CommandQueue
from gateway.sensor_collection import SensorDataCollector
from gateway.timeseries import LoggerService
from gateway.web import WebServer
from gateway.web.core import HTTPError, Request, Router, dumps
from utils.gateway_state import GatewayState
from utils.protocol import SensorReading


def readings(*pairs):
    return [SensorReading(name=n, units=u, value=v, sensor_class="BME280", timestamp=0) for n, u, v in pairs]


class TestRouter:
    def test_first_match_wins_and_params(self):
        r = Router()
        r.add("GET", "/api/status", lambda req: "status")
        r.add("GET", "/static/{path:path}", lambda req: req.params["path"])
        r.add("GET", "/{cmd}", lambda req: "cmd")
        r.add("GET", "/{cmd}/{node}", lambda req: "unicast")

        fn, params = r.resolve("GET", "/api/status")
        assert fn(None) == "status"
        fn, params = r.resolve("GET", "/static/js/app.js")
        assert params == {"path": "js/app.js"}
        fn, params = r.resolve("GET", "/ping/patio%20node")
        assert params == {"cmd": "ping", "node": "patio node"}
        with pytest.raises(HTTPError) as e:
            r.resolve("POST", "/api/status")
        assert e.value.status == 404
        with pytest.raises(HTTPError):
            r.resolve("GET", "/a/b/c")

    def test_root_route(self):
        r = Router()
        r.add("GET", "/", lambda req: "root")
        fn, params = r.resolve("GET", "/")
        assert fn(None) == "root"

    def test_request_args(self):
        req = Request("GET", "/x", {"n": ["5"], "bad": ["x"], "s": ["a", "", "b"]}, {}, b"")
        assert req.int_arg("n", lo=0, hi=3) == 3
        assert req.int_arg("missing", 7) == 7
        assert req.arg_list("s") == ["a", "b"]
        with pytest.raises(HTTPError):
            req.int_arg("bad")
        with pytest.raises(HTTPError):
            Request("POST", "/x", {}, {}, b"{oops").json()

    def test_dumps_sanitizes_nan(self):
        assert json.loads(dumps({"v": float("nan"), "w": [1.5, float("inf")]})) == {"v": None, "w": [1.5, None]}
        # Command API keeps json.dumps' historical spacing
        assert dumps({"a": 1}, compact=False) == b'{"a": 1}'


class TestFeed:
    def test_incremental_feed(self):
        c = SensorDataCollector("gw", None, feed_size=50)
        c.add_readings("patio", readings(("Temperature", "C", 20.0), ("Humidity", "%", 50.0)), rssi=-80)
        first = c.get_feed()
        assert first["reset"] and [r["name"] for r in first["readings"]] == ["Temperature", "Humidity"]
        assert first["readings"][0]["rssi"] == -80

        c.add_readings("patio", readings(("Temperature", "C", 21.0)))
        nxt = c.get_feed(after=first["seq"], epoch=first["epoch"])
        assert not nxt["reset"]
        assert [r["value"] for r in nxt["readings"]] == [21.0]
        assert c.get_feed(after=nxt["seq"], epoch=first["epoch"])["readings"] == []
        # A different epoch (gateway restarted) forces a full resend
        assert c.get_feed(after=nxt["seq"], epoch=first["epoch"] + 1)["reset"]

    def test_resize_keeps_newest(self):
        c = SensorDataCollector("gw", None, feed_size=20)
        for i in range(30):
            c.add_readings("n", readings(("V", "V", float(i))))
        assert c.set_feed_size(10) == 10
        values = [r["value"] for r in c.get_feed()["readings"]]
        assert values == [float(i) for i in range(20, 30)]

    def test_nodes_and_catalog(self):
        c = SensorDataCollector("gw", None)
        c.add_readings("gw", readings(("Temperature", "C", 20.0)), is_local=True)
        c.add_readings("patio", readings(("Temperature", "C", 10.0)), rssi=-95)
        nodes = c.get_nodes()
        assert [n["id"] for n in nodes] == ["gw", "patio"]  # local first
        assert nodes[1]["rssi"] == -95 and nodes[1]["packets"] == 1
        assert {s["id"] for s in c.get_series_catalog()} == {"gw_bme280_temperature", "patio_bme280_temperature"}

    def test_listener_receives_batches(self):
        c = SensorDataCollector("gw", None)
        seen = []
        c.set_listener(lambda dps, t: seen.append((len(dps), t)))
        c.add_readings("n", readings(("A", "", 1.0), ("B", "", 2.0)))
        assert seen and seen[0][0] == 2
        c.set_listener(None)
        c.add_readings("n", readings(("A", "", 1.0)))
        assert len(seen) == 1


@pytest.fixture
def server(tmp_path):
    state = GatewayState()
    state.node_id = "test-gw"
    queue = CommandQueue(wait_timeout=1.0)
    state.command_queue = queue
    collector = SensorDataCollector("test-gw", None)
    service = LoggerService(tmp_path / "logger", collector=collector, flush_interval=3600)
    web = WebServer(port=0, command_queue=queue, host="127.0.0.1")
    web.set_sensor_collector(collector)
    web.set_logger_service(service)
    web.set_gateway_state(state)
    web.start()
    assert web.wait_ready(10)
    yield f"http://127.0.0.1:{web.bound_port}", collector, queue
    service.stop()
    web.stop()


def put(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="PUT", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except UrlHTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read(), r.headers
    except UrlHTTPError as e:
        return e.code, e.read(), e.headers


class TestServer:
    def test_pages_and_static(self, server):
        base, _, _ = server
        status, body, headers = get(f"{base}/")
        assert status == 200 and b"<html" in body
        assert headers["ETag"]
        status, body, _ = get(f"{base}/static/js/common.js")
        assert status == 200 and b"export" in body
        status, _, _ = get(f"{base}/static/../server.py")
        assert status == 404

    def test_favicon_never_becomes_a_command(self, server):
        base, _, queue = server
        status, _, headers = get(f"{base}/favicon.ico")
        assert status == 200 and headers["Content-Type"] == "image/jpeg"
        status, _, _ = get(f"{base}/apple-touch-icon.png")
        assert status == 404
        assert queue.flush() == 0

    def test_status_and_feed(self, server):
        base, collector, _ = server
        collector.add_readings("patio", readings(("Temperature", "C", 20.0)), rssi=-70)
        status, body, _ = get(f"{base}/api/status")
        data = json.loads(body)
        assert status == 200 and data["node_id"] == "test-gw"
        assert data["nodes"][0]["id"] == "patio"
        assert data["logger"]["recording"] is False
        status, body, _ = get(f"{base}/api/readings?after=0")
        assert json.loads(body)["readings"][0]["value"] == 20.0

    def test_unicast_command_times_out_cleanly(self, server):
        base, _, _ = server
        status, body, _ = get(f"{base}/ping/patio")
        assert status == 504
        assert json.loads(body)["error"] == "timeout"

    def test_concurrent_requests_while_a_command_waits(self, server):
        base, _, _ = server
        t = threading.Thread(target=get, args=(f"{base}/ping/slow",))
        t.start()
        start = time.time()
        status, _, _ = get(f"{base}/api/status")
        assert status == 200 and time.time() - start < 0.8  # not blocked by the 1 s wait
        t.join()

    def test_logger_api_validation(self, server):
        base, _, _ = server
        status, body, _ = get(f"{base}/api/logger/data")
        assert status == 400
        status, body, _ = get(f"{base}/api/logger")
        assert status == 200 and json.loads(body)["enabled"] is False

    def test_node_order_round_trip(self, server):
        base, _, _ = server
        status, body = put(f"{base}/api/nodes/order", {"ids": ["shed", "patio", "shed"]})
        assert status == 200 and body["ids"] == ["shed", "patio"]
        _, raw, _ = get(f"{base}/api/status")
        assert json.loads(raw)["node_order"] == ["shed", "patio"]
        status, _ = put(f"{base}/api/nodes/order", {"ids": "patio"})
        assert status == 400
