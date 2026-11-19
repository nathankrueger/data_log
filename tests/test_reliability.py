"""
Reliability tests: concurrency, fault injection and recovery for the gateway's
web server, live collector and history logger.

Design rules for these tests:
  - Deterministic: fixed random seeds, exact invariants instead of timing
    (the few timing bounds are generous and noted).
  - Every thread is joined with a timeout, so a deadlock fails the test with a
    clear message instead of hanging the run.
  - Errors raised inside worker threads are collected and re-raised.

The central invariant (assert_rollups_match_raw): whatever order samples
arrive in, whatever crashes, retries or purges happen, every stored rollup
bucket must equal what you'd compute from the raw rows that survived.
"""

import http.client
import itertools
import json
import random
import socket
import sqlite3
import threading
import time
import traceback
import types
import urllib.request
from urllib.error import HTTPError as UrlHTTPError

import pytest

import gateway.sensor_collection as sc
import gateway.timeseries.recorder as recorder_mod
from gateway.command_queue import CommandQueue
from gateway.sensor_collection import SensorDataCollector
from gateway.timeseries.constants import RETENTION_MAX_S, TIERS
from gateway.timeseries.export import csv_wide, resolve_tz
from gateway.timeseries.recorder import Recorder
from gateway.timeseries.service import LoggerService
from gateway.timeseries.store import TimeSeriesStore, StaleWriterError, is_corruption
from gateway.web import WebServer
from utils.config_persistence import update_config_file
from utils.gateway_state import GatewayState
from utils.protocol import SensorReading

DAY = 86400
JOIN_TIMEOUT = 120.0


# =============================================================================
# Helpers
# =============================================================================


def run_concurrently(*fns, timeout=JOIN_TIMEOUT):
    """Run each fn in its own thread; fail on any exception or hang."""
    errors = []

    def wrap(fn):
        def inner():
            try:
                fn()
            except BaseException:  # noqa: BLE001
                errors.append(traceback.format_exc())

        return inner

    threads = [threading.Thread(target=wrap(fn), daemon=True) for fn in fns]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    hung = [t for t in threads if t.is_alive()]
    assert not hung, f"{len(hung)} worker thread(s) did not finish (deadlock?)"
    assert not errors, errors[0]


class Background:
    """Run loop bodies repeatedly in threads until the block exits."""

    def __init__(self, *bodies):
        self.stop = threading.Event()
        self.errors = []

        def wrap(body):
            def inner():
                try:
                    while not self.stop.is_set():
                        body()
                except BaseException:  # noqa: BLE001
                    self.errors.append(traceback.format_exc())

            return inner

        self.threads = [threading.Thread(target=wrap(b), daemon=True) for b in bodies]

    def __enter__(self):
        for t in self.threads:
            t.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        for t in self.threads:
            t.join(JOIN_TIMEOUT)
        assert not any(t.is_alive() for t in self.threads), "background thread hung"
        assert not self.errors, self.errors[0]


def readings(*pairs):
    return [SensorReading(name=n, units=u, value=v, sensor_class="BME280", timestamp=0) for n, u, v in pairs]


def dp(key, value):
    return {"id": key, "value": value}


def make_recorder(store, keys, retention=30 * DAY):
    rec = Recorder(store, flush_interval=3600)
    rec.set_series(store.ensure_series([(k, "n", k, "u", retention) for k in keys]))
    return rec


def rollups(store, key):
    """(raw rows, stored buckets, buckets recomputed from the raw rows)."""
    sid = store.lookup([key])[key]
    with store._reader() as conn:
        raw = conn.execute("SELECT ts, v FROM raw WHERE sid = ?", (sid,)).fetchall()
        stored = {
            (tier, ts): (n, lo, hi, total)
            for tier, ts, n, lo, hi, total in conn.execute(
                "SELECT tier, ts, n, vmin, vmax, vsum FROM agg WHERE sid = ?", (sid,)
            )
        }
    expected = {}
    for ts_ms, v in raw:
        sec = ts_ms // 1000
        for tier in TIERS:
            k = (tier, sec - sec % tier)
            e = expected.get(k)
            expected[k] = (1, v, v, v) if e is None else (e[0] + 1, min(e[1], v), max(e[2], v), e[3] + v)
    return raw, stored, expected


def assert_rollups_match_raw(store, key):
    raw, stored, expected = rollups(store, key)
    assert stored.keys() == expected.keys(), "rollup buckets differ from raw data"
    for k, (n, lo, hi, total) in expected.items():
        assert stored[k][:3] == (n, lo, hi), (k, stored[k], expected[k])
        assert stored[k][3] == pytest.approx(total), (k, stored[k], expected[k])
    return raw


def get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(), r.headers
    except UrlHTTPError as e:
        return e.code, e.read(), e.headers


def recv_all(sock):
    chunks = []
    while True:
        try:
            data = sock.recv(65536)
        except (ConnectionResetError, socket.timeout):
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def recorder_threads():
    return [t for t in threading.enumerate() if t.name == "HistoryRecorder" and t.is_alive()]


@pytest.fixture
def store(tmp_path):
    s = TimeSeriesStore(tmp_path / "ts.db")
    s.open_writer()
    yield s
    s.close_writer()


def seeded_collector(*names, units="C"):
    c = SensorDataCollector("gw", None)
    c.add_readings("n", readings(*[(name, units, 1.0) for name in names]))
    return c, [f"n_bme280_{name.lower()}" for name in names]


# =============================================================================
# Rollup invariants under disorder, crashes and failures
# =============================================================================


class TestRollupInvariants:
    @pytest.mark.parametrize("seed", range(6))
    def test_shuffled_ingest_with_crashes_keeps_rollups_exact(self, store, seed):
        rng = random.Random(seed)
        rec = make_recorder(store, ["k"])
        sids = store.lookup(["k"])
        base = 1_700_000_000.0
        times = [base + i * 23.0 + rng.random() for i in range(800)]  # unique, ~5 h
        for _ in range(300):  # local and long-range disorder (clock steps)
            i, j = rng.randrange(len(times)), rng.randrange(len(times))
            times[i], times[j] = times[j], times[i]
        for t in times:
            rec.ingest([dp("k", float(rng.randrange(-40, 41)))], t)
            r = rng.random()
            if r < 0.04:
                rec.flush()
            elif r < 0.05:
                # Crash: the buffer is lost and a fresh recorder (no in-memory
                # accumulators) takes over the same database
                rec = Recorder(store, flush_interval=3600)
                rec.set_series(sids)
        rec.flush()
        raw = assert_rollups_match_raw(store, "k")
        assert 0 < len(raw) <= len(times)

    def test_failed_write_is_retried_without_double_counting(self, store, monkeypatch):
        rec = make_recorder(store, ["k"])
        base = 1_700_000_000.0
        for i in range(50):
            rec.ingest([dp("k", float(i))], base + i * 7)
        real = store.write_batch
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("disk I/O error")
            return real(*args, **kwargs)

        monkeypatch.setattr(store, "write_batch", flaky)
        with pytest.raises(sqlite3.OperationalError):
            rec.flush()
        assert rec.pending_count == 50  # requeued, nothing lost
        for i in range(50, 60):
            rec.ingest([dp("k", float(i))], base + i * 7)
        assert rec.flush() == 60
        assert len(assert_rollups_match_raw(store, "k")) == 60

    def test_failure_while_loading_a_bucket_is_retried_cleanly(self, store, monkeypatch):
        rec = make_recorder(store, ["k"])
        base = 1_700_000_000.0
        for i in range(30):
            rec.ingest([dp("k", float(i))], base + i * 11)
        rec.flush()
        rec2 = Recorder(store)  # fresh accumulators: will load buckets from disk
        rec2.set_series(store.lookup(["k"]))
        real = store.load_bucket
        calls = {"n": 0}

        def flaky(*args):
            calls["n"] += 1
            if calls["n"] == 2:  # fail mid-aggregation, after some state changed
                raise sqlite3.OperationalError("disk I/O error")
            return real(*args)

        monkeypatch.setattr(store, "load_bucket", flaky)
        for i in range(30, 40):
            rec2.ingest([dp("k", float(i))], base + i * 11)
        with pytest.raises(sqlite3.OperationalError):
            rec2.flush()
        assert rec2.flush() == 10
        assert len(assert_rollups_match_raw(store, "k")) == 40

    def test_pending_buffer_is_bounded_when_the_disk_stalls(self, store, monkeypatch):
        monkeypatch.setattr(recorder_mod, "MAX_PENDING", 100)
        rec = make_recorder(store, ["k"])

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("database or disk is full")

        monkeypatch.setattr(store, "write_batch", broken)
        base = 1_700_000_000.0
        for i in range(150):
            rec.ingest([dp("k", 1.0)], base + i)
        assert rec.pending_count == 100 and rec.samples_dropped == 50
        with pytest.raises(sqlite3.OperationalError):
            rec.flush()
        assert rec.pending_count == 100
        for i in range(150, 170):
            rec.ingest([dp("k", 1.0)], base + i)
        assert rec.pending_count == 100
        newest = rec.unflushed_snapshot()[store.lookup(["k"])["k"]][-1]
        assert newest[0] == int((base + 169) * 1000)  # the newest samples are kept

    def test_corruption_errors_flag_the_recorder(self, store, monkeypatch):
        rec = make_recorder(store, ["k"])
        rec.ingest([dp("k", 1.0)], 1_700_000_000.0)

        def corrupt(*args, **kwargs):
            raise sqlite3.DatabaseError("database disk image is malformed")

        monkeypatch.setattr(store, "write_batch", corrupt)
        rec._guard(rec.flush)
        assert rec.corrupt and "malformed" in rec.last_error


# =============================================================================
# Store / recorder concurrency
# =============================================================================


class TestRecorderConcurrency:
    def test_producers_flusher_and_readers(self, store):
        keys = [f"s{i}" for i in range(4)]
        rec = make_recorder(store, keys)
        base = time.time() - 4 * 3600
        per = 300

        def producer(idx):
            def run():
                for i in range(per):
                    rec.ingest([dp(keys[idx], float(i % 13))], base + i * 3.0 + idx * 0.01)

            return run

        def flush():
            rec.flush()
            time.sleep(0.005)

        def read():
            res = store.query(keys, base - 1, base + per * 3 + 1, 200, unflushed=rec.unflushed_snapshot())
            for col in res["series"].values():
                assert col["t"] == sorted(col["t"])
                assert len(col["t"]) == len(set(col["t"]))  # no duplicates from the buffer merge
            store.list_series()

        with Background(flush, read, read):
            run_concurrently(*(producer(i) for i in range(len(keys))))
        rec.flush()
        for k in keys:
            assert len(assert_rollups_match_raw(store, k)) == per

    def test_purge_racing_with_ingest_and_flush(self, store):
        rec = make_recorder(store, ["k"])
        base = time.time() - 3600

        def produce():
            for i in range(1500):
                rec.ingest([dp("k", float(i % 7))], base + i * 1.5)

        def flush():
            rec.flush()
            time.sleep(0.003)

        def purge():
            rec.purge(["k"])
            time.sleep(0.02)

        with Background(flush, purge):
            run_concurrently(produce)
        rec.flush()
        assert_rollups_match_raw(store, "k")

    def test_export_streams_while_recording(self, store):
        rec = make_recorder(store, ["a", "b"])
        base = time.time() - 7200
        for i in range(2000):
            rec.ingest([dp("a", float(i)), dp("b", float(-i))], base + i)
        rec.flush()
        tz, label = resolve_tz(None, 0)
        series = [{"id": "a"}, {"id": "b"}]

        def more():
            for i in range(2000, 3000):
                rec.ingest([dp("a", 1.0), dp("b", 2.0)], base + i)
                if i % 50 == 0:
                    rec.flush()

        def export():
            for _ in range(4):
                text = b"".join(csv_wide(store, series, base - 1, base + 4000, tz, label)).decode()
                lines = text.strip().split("\n")
                assert len(lines) >= 2001
                prev = -1.0
                for line in lines[1:]:
                    t = float(line.split(",")[1])
                    assert t > prev  # one row per timestamp, in order
                    prev = t

        run_concurrently(more, export, export)

    def test_start_stop_cycles_lose_nothing(self, store):
        sids = store.ensure_series([("k", "n", "k", "u", 30 * DAY)])
        base = time.time() - 3600
        total = 0
        for cycle in range(8):
            rec = Recorder(store, flush_interval=0.02, prune_interval=3600)
            rec.set_series(sids)
            rec.start()
            for i in range(25):
                rec.ingest([dp("k", 1.0)], base + total + i)
            total += 25
            if cycle % 2:
                time.sleep(0.05)  # let a periodic flush happen on odd cycles
            rec.stop()
            assert not rec.is_alive()
            assert rec.pending_count == 0  # the final flush ran
        assert len(assert_rollups_match_raw(store, "k")) == total
        Recorder(store).stop()  # stopping a never-started recorder is a no-op


# =============================================================================
# Collector (LoRa / local-sensor threads vs. web threads)
# =============================================================================


class TestCollectorConcurrency:
    def test_producers_resizing_and_incremental_readers(self):
        c = SensorDataCollector("gw", None, feed_size=500)
        producers, per = 4, 400

        def producer(k):
            def run():
                for i in range(per):
                    c.add_readings(f"node{k}", readings(("A", "u", float(i)), ("B", "u", float(i))), rssi=-80)

            return run

        def make_reader():
            st = {"after": 0, "epoch": None, "seen": set()}

            def read():
                feed = c.get_feed(after=st["after"], epoch=st["epoch"])
                seqs = [r["seq"] for r in feed["readings"]]
                assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
                if st["after"] > 0:  # after=0 legitimately means "send everything"
                    assert not feed["reset"]
                    assert all(s > st["after"] for s in seqs)
                    assert not set(seqs) & st["seen"]  # never delivered twice
                st["seen"].update(seqs)
                st["after"], st["epoch"] = feed["seq"], feed["epoch"]

            return read

        sizes = itertools.cycle([50, 2000, 10, 500])

        def resize():
            c.set_feed_size(next(sizes))
            time.sleep(0.001)

        def inspect():
            c.get_nodes()
            c.get_series_catalog()
            c.feed_info()

        with Background(make_reader(), make_reader(), resize, inspect):
            run_concurrently(*(producer(k) for k in range(producers)))

        packets = {n["id"]: n["packets"] for n in c.get_nodes()}
        assert packets == {f"node{k}": per for k in range(producers)}
        assert c.feed_info()["seq"] == producers * per * 2

    def test_bookkeeping_failures_never_block_dashboard_posting(self, monkeypatch):
        c = SensorDataCollector("gw", dashboard_client=object())  # poster not started: posts queue up

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(c, "_update_live_state", boom)
        c.set_listener(boom)
        c.add_readings("n", readings(("A", "u", 1.0)))
        assert c._post_queue.qsize() == 1

    def test_live_catalogs_are_bounded(self, monkeypatch):
        monkeypatch.setattr(sc, "MAX_NODES", 3)
        monkeypatch.setattr(sc, "MAX_SERIES", 4)
        clock = itertools.count(1_700_000_000)
        monkeypatch.setattr(sc, "time", types.SimpleNamespace(time=lambda: float(next(clock)), sleep=time.sleep))
        c = SensorDataCollector("gw", None)
        for k in range(6):
            c.add_readings(f"n{k}", readings(("A", "u", 1.0)))
        assert sorted(n["id"] for n in c.get_nodes()) == ["n3", "n4", "n5"]
        assert len(c.get_series_catalog()) == 4
        # Hearing an old node again keeps it; the least recently heard goes
        c.add_readings("n3", readings(("A", "u", 2.0)))
        c.add_readings("n6", readings(("A", "u", 1.0)))
        assert sorted(n["id"] for n in c.get_nodes()) == ["n3", "n5", "n6"]


# =============================================================================
# Logger service: lifecycle races, watchdog, corruption recovery
# =============================================================================


class TestServiceReliability:
    def test_toggling_recording_under_load(self, tmp_path):
        c, (key,) = seeded_collector("T")
        svc = LoggerService(tmp_path / "lg", collector=c, flush_interval=0.02, prune_interval=3600)
        svc.create_chart({"series": [key], "max_age_s": DAY})
        vals = itertools.count()

        def produce():
            c.add_readings("n", readings(("T", "C", float(next(vals) % 10))))
            time.sleep(0.002)  # keep receive times on distinct milliseconds

        def read():
            now = time.time()
            svc.state()
            svc.summary()
            svc.query([key], now - 120, now + 1, 200)

        def toggle():
            for i in range(16):
                svc.set_enabled(i % 2 == 0)
                time.sleep(0.01)

        with Background(produce, read, read):
            run_concurrently(toggle)
        svc.set_enabled(False)
        assert not svc.recording and c._listener is None
        assert not recorder_threads()
        assert_rollups_match_raw(TimeSeriesStore(tmp_path / "lg" / "timeseries.db"), key)

    def test_concurrent_chart_edits_persist_consistently(self, tmp_path):
        c, keys = seeded_collector(*[f"T{i}" for i in range(8)])
        svc = LoggerService(tmp_path / "lg", collector=c, flush_interval=0.05)
        svc.set_enabled(True)

        def editor(seed):
            def run():
                rng = random.Random(seed)
                mine = []
                for _ in range(7):  # at most 28 creates in total: under MAX_CHARTS
                    op = rng.random()
                    if op < 0.4 or not mine:
                        chart = svc.create_chart({
                            "series": rng.sample(keys, rng.randint(1, 3)),
                            "max_age_s": rng.choice([3600, DAY, 7 * DAY]),
                        })
                        mine.append(chart["id"])
                    elif op < 0.7:
                        svc.update_chart(rng.choice(mine), {"series": rng.sample(keys, rng.randint(1, 4)), "title": f"t{seed}"})
                    elif op < 0.85:
                        svc.delete_chart(mine.pop(rng.randrange(len(mine))))
                    else:
                        ids = [ch["id"] for ch in svc.state()["charts"]]
                        rng.shuffle(ids)
                        svc.reorder(ids)

            return run

        def watch():
            svc.state()
            time.sleep(0.001)

        with Background(watch):
            run_concurrently(*(editor(s) for s in range(4)))

        final = svc.state()["charts"]
        assert set(svc._recorder.recorded_keys) == set(svc._retention_map())
        svc.stop()
        reloaded = LoggerService(tmp_path / "lg", collector=SensorDataCollector("gw", None))
        assert reloaded.state()["charts"] == final

    def test_watchdog_restarts_a_dead_recorder(self, tmp_path):
        c, (key,) = seeded_collector("T")
        svc = LoggerService(tmp_path / "lg", collector=c, flush_interval=0.05)
        svc.create_chart({"series": [key]})
        svc.set_enabled(True)
        old = svc._recorder
        old.stop()  # the thread "dies"
        assert not svc.recording
        svc.check_health()
        assert svc.recording and svc._recorder is not old
        assert c._listener == svc._recorder.ingest
        svc.stop()
        assert not recorder_threads()

    def test_watchdog_backs_off_when_restarts_keep_failing(self, tmp_path, monkeypatch):
        c, (key,) = seeded_collector("T")
        svc = LoggerService(tmp_path / "lg", collector=c)
        svc.create_chart({"series": [key]})
        svc.set_enabled(True)
        svc._recorder.stop()
        calls = []

        def failing_start():
            calls.append(1)
            raise OSError("disk gone")

        monkeypatch.setattr(svc, "_start_recorder", failing_start)
        for _ in range(3):
            svc.check_health()
        assert len(calls) == 1  # later checks wait out the backoff
        svc._next_restart = 0.0  # backoff elapsed
        svc.check_health()
        assert len(calls) == 2
        assert svc._restart_backoff > 60

    def test_corrupt_database_is_moved_aside_on_start(self, tmp_path):
        data = tmp_path / "lg"
        data.mkdir()
        (data / "timeseries.db").write_bytes(b"this is not a sqlite database " * 200)
        c, (key,) = seeded_collector("T")
        svc = LoggerService(data, collector=c, flush_interval=3600)
        svc.create_chart({"series": [key]})
        svc.set_enabled(True)
        assert svc.recording
        assert list(data.glob("timeseries.db.corrupt-*"))
        c.add_readings("n", readings(("T", "C", 5.0)))
        svc.set_enabled(False)
        now = time.time()
        assert svc.query([key], now - 60, now + 1, 100)["series"][key]["v"] == [5.0]

    def test_watchdog_recovers_from_runtime_corruption(self, tmp_path):
        data = tmp_path / "lg"
        c, (key,) = seeded_collector("T")
        svc = LoggerService(data, collector=c, flush_interval=3600)
        svc.create_chart({"series": [key]})
        svc.set_enabled(True)
        svc._recorder.corrupt = True  # as if a flush had hit SQLITE_CORRUPT
        svc.check_health()
        assert svc.recording and not svc._recorder.corrupt
        assert list(data.glob("timeseries.db.corrupt-*"))
        svc.stop()

    def test_corrupt_config_is_moved_aside(self, tmp_path):
        data = tmp_path / "lg"
        data.mkdir()
        (data / "logger.json").write_text("{not json")
        svc = LoggerService(data)
        assert svc.state()["charts"] == [] and not svc.enabled
        assert list(data.glob("logger.json.corrupt-*"))

    def test_junk_config_entries_are_repaired_or_dropped(self, tmp_path):
        data = tmp_path / "lg"
        data.mkdir()
        (data / "logger.json").write_text(json.dumps({
            "enabled": "yes",
            "charts": [
                {
                    "id": "ok",
                    "title": "T",
                    "units": "C",
                    "max_age_s": 10**12,
                    "series": [{"id": "a", "color": 99}, {"id": "a"}, {"id": "b", "color": 0}, "junk"],
                },
                {"id": "", "series": [{"id": "x"}]},
                {"series": [{"id": "y"}]},
                {"id": "empty", "series": []},
                "not a chart",
                {"id": "ok", "series": [{"id": "dupe"}]},
            ],
            "series_meta": {"a": {"units": "C"}, "b": "bad"},
        }))
        svc = LoggerService(data)
        assert not svc.enabled  # only a real true enables recording
        charts = svc.state()["charts"]
        assert [ch["id"] for ch in charts] == ["ok"]
        assert charts[0]["series"] == [{"id": "a", "color": 1}, {"id": "b", "color": 0}]
        assert charts[0]["max_age_s"] == RETENTION_MAX_S

    def test_corruption_classification(self, tmp_path):
        bad = tmp_path / "bad.db"
        bad.write_bytes(b"x" * 4096)
        conn = sqlite3.connect(str(bad))
        with pytest.raises(sqlite3.DatabaseError) as err:
            conn.execute("PRAGMA user_version").fetchone()
        conn.close()
        assert is_corruption(err.value)
        assert is_corruption(sqlite3.DatabaseError("database disk image is malformed"))
        assert not is_corruption(sqlite3.OperationalError("database is locked"))
        assert not is_corruption(OSError("disk"))


def test_concurrent_config_updates_are_not_lost(tmp_path):
    path = tmp_path / "gateway_config.json"
    path.write_text("{}")

    def writer(i):
        return lambda: [update_config_file(str(path), {f"k{i}.n{j}": j}) for j in range(25)]

    run_concurrently(*(writer(i) for i in range(6)))
    data = json.loads(path.read_text())
    assert all(data[f"k{i}"][f"n{j}"] == j for i in range(6) for j in range(25))


# =============================================================================
# Web server
# =============================================================================


@pytest.fixture
def make_server(tmp_path):
    started = []

    def factory(port=0, wait_timeout=0.5, **kwargs):
        state = GatewayState()
        state.node_id = "gw"
        queue = CommandQueue(wait_timeout=wait_timeout)
        state.command_queue = queue
        collector = SensorDataCollector("gw", None)
        service = LoggerService(tmp_path / f"lg{len(started)}", collector=collector, flush_interval=3600)
        web = WebServer(port=port, command_queue=queue, host="127.0.0.1", **kwargs)
        web.set_sensor_collector(collector)
        web.set_logger_service(service)
        web.set_gateway_state(state)
        web.start()
        started.append((web, service))
        return web, collector, service

    yield factory
    for web, service in started:
        service.stop()
        web.stop()


def base_url(web):
    assert web.wait_ready(10), "web server did not come up"
    return f"http://127.0.0.1:{web.bound_port}"


class TestWebServerReliability:
    def test_many_concurrent_clients_while_readings_arrive(self, make_server):
        web, collector, _ = make_server()
        base = base_url(web)
        baseline = threading.active_count()
        paths = ["/api/status", "/api/readings?after=0", "/static/js/common.js", "/", "/api/logger", "/logger"]

        def produce():
            collector.add_readings("n", readings(("A", "u", 1.0)))
            time.sleep(0.002)

        def client(seed):
            def run():
                rng = random.Random(seed)
                for _ in range(20):
                    status, _, _ = get(base + rng.choice(paths))
                    assert status == 200

            return run

        with Background(produce):
            run_concurrently(*(client(s) for s in range(8)))
        # Handler threads exit once their connections close: no thread leak
        deadline = time.monotonic() + 10
        while threading.active_count() > baseline and time.monotonic() < deadline:
            time.sleep(0.05)
        assert threading.active_count() <= baseline

    def test_keep_alive_connection_survives_errors(self, make_server):
        web, _, _ = make_server()
        base_url(web)
        conn = http.client.HTTPConnection("127.0.0.1", web.bound_port, timeout=10)
        steps = [
            ("GET", "/api/status", None, 200),
            ("POST", "/no/such/route", b'{"x": 1}', 404),  # unread body must be drained
            ("PUT", "/api/feed", b"{oops", 400),
            ("GET", "/static/css/app.css", None, 200),
            ("GET", "/api/readings?after=0", None, 200),
        ]
        for method, path, body, expected in steps:
            headers = {"Content-Type": "application/json"} if body else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            resp.read()
            assert resp.status == expected, (method, path)
        conn.close()

    def test_connection_cap_sheds_excess_connections(self, make_server):
        web, _, _ = make_server(max_connections=3)
        base = base_url(web)
        idle = [socket.create_connection(("127.0.0.1", web.bound_port), timeout=10) for _ in range(3)]
        deadline = time.monotonic() + 10
        while web._server._slots._value > 0:  # wait until all three hold a slot
            assert time.monotonic() < deadline, "server never accepted the idle connections"
            time.sleep(0.02)

        extra = socket.create_connection(("127.0.0.1", web.bound_port), timeout=10)
        try:
            extra.sendall(b"GET /api/status HTTP/1.1\r\nHost: x\r\n\r\n")
            data = recv_all(extra)
        except (ConnectionResetError, BrokenPipeError):
            data = b""
        extra.close()
        # Refused without a handler thread: a retryable 503 (or, if the reset
        # beat the response, a closed connection)
        assert data == b"" or data.startswith(b"HTTP/1.1 503")
        assert web._server._slots._value == 0  # the idle three still hold their slots

        for s in idle:
            s.close()
        deadline = time.monotonic() + 10
        while True:  # slots free up as soon as the idle connections end
            try:
                if get(base + "/api/status")[0] == 200:
                    break
            except OSError:
                pass
            assert time.monotonic() < deadline, "server did not recover after the idle connections closed"
            time.sleep(0.1)

    def test_waits_for_a_busy_port_then_binds(self, make_server):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        web, _, _ = make_server(port=port)
        assert not web.wait_ready(1.5)
        blocker.close()
        assert web.wait_ready(15)
        assert get(f"http://127.0.0.1:{port}/api/status")[0] == 200

    def test_stop_while_waiting_for_the_port_is_prompt(self, make_server):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        web, _, _ = make_server(port=blocker.getsockname()[1])
        time.sleep(0.2)
        t0 = time.monotonic()
        web.stop()
        assert time.monotonic() - t0 < 5 and not web.is_alive()
        blocker.close()

    def test_handler_crash_is_a_500_and_serving_continues(self, make_server, monkeypatch):
        web, collector, _ = make_server()
        base = base_url(web)

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(collector, "get_nodes", boom)
        status, body, _ = get(base + "/api/status")
        assert status == 500 and json.loads(body)["error"] == "internal"
        assert get(base + "/api/readings?after=0")[0] == 200

    def test_hostile_requests_are_contained(self, make_server):
        web, _, _ = make_server()
        base = base_url(web)
        addr = ("127.0.0.1", web.bound_port)

        s = socket.create_connection(addr, timeout=10)  # oversized body announced
        s.sendall(b"PUT /api/feed HTTP/1.1\r\nHost: x\r\nContent-Length: 5000000\r\n\r\n")
        assert recv_all(s).startswith(b"HTTP/1.1 413")
        s.close()

        s = socket.create_connection(addr, timeout=10)  # unknown method
        s.sendall(b"BREW /pot HTTP/1.1\r\nHost: x\r\n\r\n")
        assert recv_all(s).startswith(b"HTTP/1.1 501")
        s.close()

        s = socket.create_connection(addr, timeout=10)  # garbage
        s.sendall(b"\x00\x01\x02 garbage\r\n\r\n")
        recv_all(s)
        s.close()

        s = socket.create_connection(addr, timeout=10)  # client vanishes mid-request
        s.sendall(b"GET /api/status HTTP/1.1\r\nHost:")
        s.close()

        assert get(base + "/api/logger/data?s=x&start=-1e300&end=1e300")[0] == 200  # clamped
        assert get(base + "/api/logger/data?s=x&start=nan")[0] == 400
        many = "&".join(f"s=k{i}" for i in range(33))
        assert get(base + f"/api/logger/data?{many}")[0] == 400
        assert get(base + "/api/status")[0] == 200  # still healthy

    def test_ui_stays_responsive_while_commands_wait(self, make_server):
        web, _, _ = make_server(wait_timeout=2.0)
        base = base_url(web)
        waiters = [threading.Thread(target=get, args=(f"{base}/ping/node{i}",), daemon=True) for i in range(10)]
        for t in waiters:
            t.start()
        time.sleep(0.2)
        t0 = time.monotonic()
        assert get(base + "/api/status")[0] == 200
        assert time.monotonic() - t0 < 1.5  # generous: the 10 waits take 2 s each
        for t in waiters:
            t.join(15)
        assert not any(t.is_alive() for t in waiters)

    def test_csv_export_framing_for_http11_and_http10(self, make_server):
        web, collector, service = make_server()
        base = base_url(web)
        collector.add_readings("n", readings(("A", "u", 1.0)))  # seed the catalog (not recorded)
        service.create_chart({"series": ["n_bme280_a"]})
        service.set_enabled(True)
        collector.add_readings("n", readings(("A", "u", 2.0)))
        now = time.time()
        path = f"/api/logger/export.csv?s=n_bme280_a&start={now - 60:.3f}&end={now + 1:.3f}&tzo=0"

        status, body, headers = get(base + path)  # HTTP/1.1: chunked, decoded by urllib
        assert status == 200 and headers["Transfer-Encoding"] == "chunked"
        lines = body.decode().strip().split("\n")
        assert lines[0].startswith("time (UTC+00:00),unix_s,") and len(lines) == 2
        assert lines[1].endswith(",2.0")

        s = socket.create_connection(("127.0.0.1", web.bound_port), timeout=10)
        s.sendall(f"GET {path} HTTP/1.0\r\n\r\n".encode())
        head, _, payload = recv_all(s).partition(b"\r\n\r\n")
        s.close()
        assert b"chunked" not in head.lower()  # HTTP/1.0 can't take chunked framing
        assert payload.decode().strip().split("\n") == lines


# =============================================================================
# Regression tests for the adversarial review
# =============================================================================


class TestStorageFaults:
    def test_real_disk_full_rolls_back_and_recovers(self, store):
        """SQLITE_FULL from SQLite itself (not a mock): the transaction rolls
        back cleanly, the batch is kept, and rollups stay exact."""
        rec = make_recorder(store, ["k"])
        base = 1_700_000_000.0
        for i in range(20):
            rec.ingest([dp("k", float(i))], base + i)
        rec.flush()
        (pages,) = store._wconn.execute("PRAGMA page_count").fetchone()
        store._wconn.execute(f"PRAGMA max_page_count = {pages}")  # no room to grow
        for i in range(20, 4000):
            rec.ingest([dp("k", float(i % 50))], base + i)
        with pytest.raises(sqlite3.OperationalError):
            rec.flush()
        assert rec.consecutive_failures == 1 and rec.pending_count == 3980
        assert not store._wconn.in_transaction  # rolled back, connection usable
        store._wconn.execute("PRAGMA max_page_count = 1073741823")
        assert rec.flush() == 3980
        assert rec.consecutive_failures == 0 and rec.last_error is None
        assert len(assert_rollups_match_raw(store, "k")) == 4000

    def test_explicit_tier_cannot_request_unbounded_rows(self, store):
        rec = make_recorder(store, ["k"], retention=400 * DAY)
        base = time.time() - 10 * DAY
        for i in range(0, 10 * 86400, 60):  # one sample a minute for 10 days
            rec.ingest([dp("k", 1.0)], base + i)
        rec.flush()
        res = store.query(["k"], 0, 1e10, 1000, tier=60)
        assert res["tier"] > 60  # coarsened for the huge window
        assert len(res["series"]["k"]["t"]) <= 2 * 4000 + 2
        raw = store.query(["k"], base, base + 10 * DAY, 1000, tier=0)
        assert len(raw["series"]["k"]["t"]) <= 2 * 4000 + 2

    def test_abandoned_export_returns_its_reader(self, store):
        rec = make_recorder(store, ["a"])
        for i in range(5000):
            rec.ingest([dp("a", float(i))], 1_700_000_000.0 + i)
        rec.flush()
        tz, label = resolve_tz(None, 0)
        gen = csv_wide(store, [{"id": "a"}], 0, 2e9, tz, label)
        next(gen)  # client starts downloading...
        gen.close()  # ...and disconnects
        assert len(store._pool) == 1  # reader went back to the pool, not leaked

    def test_prune_is_held_after_a_forward_clock_jump(self, store, monkeypatch):
        rec = make_recorder(store, ["k"], retention=DAY)
        now = time.time()
        rec.ingest([dp("k", 1.0)], now - 3600)
        rec.flush()
        wall = {"offset": 0.0}
        fake = types.SimpleNamespace(
            time=lambda: time.time() + wall["offset"], monotonic=time.monotonic, sleep=time.sleep
        )
        monkeypatch.setattr(recorder_mod, "time", fake)
        monkeypatch.setattr(store, "prune", lambda now=None: TimeSeriesStore.prune(store, fake.time()))
        rec._clock_ref = (fake.time(), time.monotonic())
        wall["offset"] = 10 * 365 * DAY  # bogus jump ten years ahead
        assert rec.prune() == 0  # held: history survives
        assert store.query(["k"], now - 7200, now, 100)["series"]["k"]["v"] == [1.0]
        rec._prune_hold_until = 0.0  # 24 h later, the new time is trusted
        assert rec.prune() > 0


class TestServiceFaults:
    def test_alive_but_failing_recorder_is_restarted(self, tmp_path, monkeypatch):
        c, (key,) = seeded_collector("T")
        svc = LoggerService(tmp_path / "lg", collector=c, flush_interval=3600)
        svc.create_chart({"series": [key]})
        svc.set_enabled(True)
        old = svc._recorder

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(svc._store, "write_batch", broken)
        for _ in range(5):
            c.add_readings("n", readings(("T", "C", 1.0)))
            time.sleep(0.002)
            with pytest.raises(sqlite3.OperationalError):
                old.flush()
        assert svc.summary()["recording"] and "disk I/O error" in svc.summary()["error"]
        monkeypatch.undo()  # the disk recovers
        svc.check_health()
        assert svc._recorder is not old and svc.recording
        c.add_readings("n", readings(("T", "C", 9.0)))
        assert svc._recorder.flush() >= 1
        assert svc.summary()["error"] is None
        svc.stop()

    def test_retired_recorder_cannot_write(self, tmp_path):
        c, (key,) = seeded_collector("T")
        svc = LoggerService(tmp_path / "lg", collector=c, flush_interval=3600)
        svc.create_chart({"series": [key]})
        svc.set_enabled(True)
        old = svc._recorder
        svc.set_enabled(False)
        svc.set_enabled(True)  # a new recorder owns the database now
        old.ingest([dp(key, 123.0)], time.time())
        with pytest.raises(StaleWriterError):
            old.flush()
        now = time.time()
        assert 123.0 not in svc.query([key], now - 60, now + 1, 100)["series"].get(key, {}).get("v", [])
        svc.stop()

    def test_quarantine_while_an_export_holds_a_reader(self, tmp_path):
        data = tmp_path / "lg"
        c, (key,) = seeded_collector("T")
        svc = LoggerService(data, collector=c, flush_interval=3600)
        svc.create_chart({"series": [key]})
        svc.set_enabled(True)
        for i in range(3000):
            svc._recorder.ingest([dp(key, 1.0)], time.time() - 3000 + i)
        svc._recorder.flush()
        export = svc.export_csv([key], 0, 2e9, None, 0)
        next(export)  # holds a reader on the current file
        svc._recorder.corrupt = True
        svc.check_health()  # quarantines the file and starts a fresh one
        for _ in export:  # the in-flight export finishes from the old file
            pass
        svc._recorder.ingest([dp(key, 7.0)], time.time())
        svc._recorder.flush()
        now = time.time()
        for _ in range(3):  # no stale pooled reader serves the moved-aside file
            assert svc.query([key], now - 4000, now + 1, 200)["series"][key]["v"] == [7.0]
        svc.stop()


class TestConfigDurability:
    def test_failed_write_leaves_original_and_no_temp_files(self, tmp_path, monkeypatch):
        import utils.config_persistence as cp

        path = tmp_path / "gateway_config.json"
        path.write_text('{"a": 1}')

        def failing_fsync(fd):
            raise OSError("I/O error")  # fails after the temp file exists

        monkeypatch.setattr(cp.os, "fsync", failing_fsync)
        assert update_config_file(str(path), {"b": 2}) is False
        assert json.loads(path.read_text()) == {"a": 1}
        assert not list(tmp_path.glob("*.tmp"))

    def test_truncated_config_falls_back_to_backup(self, tmp_path):
        from gateway.server import load_config

        path = tmp_path / "gateway_config.json"
        path.write_text('{"node_id": "gw", "web": {}}')
        assert update_config_file(str(path), {"web.feed_size": 500})
        path.write_text("")  # power cut mid-write on a filesystem without the fix
        assert load_config(str(path)) == {"node_id": "gw", "web": {}}  # previous version (.bak)
        assert update_config_file(str(path), {"web.feed_size": 700})  # repairs the file
        assert json.loads(path.read_text())["web"]["feed_size"] == 700


class TestWebServerFaults:
    def test_idle_keep_alive_connections_release_their_slots(self, make_server):
        web, _, _ = make_server(max_connections=2, request_timeout=0.5)
        base = base_url(web)
        idle = [socket.create_connection(("127.0.0.1", web.bound_port), timeout=10) for _ in range(2)]
        try:
            time.sleep(1.5)  # both idle past the request timeout
            assert get(base + "/api/status")[0] == 200
        finally:
            for s in idle:
                s.close()

    def test_stop_during_startup_does_not_leave_a_server_running(self, make_server):
        for _ in range(5):
            web, _, _ = make_server()
            web.stop()
            assert not web.is_alive()
