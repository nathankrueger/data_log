"""Tests for the on-gateway history logger (gateway/timeseries)."""

import time

import pytest

from gateway.sensor_collection import SensorDataCollector
from gateway.timeseries.constants import TIERS
from gateway.timeseries.export import csv_long, csv_wide, resolve_tz, safe_filename
from gateway.timeseries.recorder import Recorder
from gateway.timeseries.service import LoggerService, NotFound
from gateway.timeseries.store import TimeSeriesStore, pick_tier
from utils.protocol import SensorReading

DAY = 86400


@pytest.fixture
def store(tmp_path):
    s = TimeSeriesStore(tmp_path / "ts.db")
    s.open_writer()
    yield s
    s.close_writer()


@pytest.fixture
def recorder(store):
    rec = Recorder(store, flush_interval=3600)
    sids = store.ensure_series([
        ("a_bme_temp", "a", "Temperature", "C", 7 * DAY),
        ("b_bme_temp", "b", "Temperature", "C", 7 * DAY),
    ])
    rec.set_series(sids)
    return rec


def dp(key, value):
    return {"id": key, "value": value}


def agg_row(store, key, tier, ts):
    sid = store.lookup([key])[key]
    return store.load_bucket(sid, tier, ts)


class TestStoreBasics:
    def test_ensure_series_is_idempotent(self, store):
        first = store.ensure_series([("k1", "n", "T", "C", 3600)])
        again = store.ensure_series([("k1", "", "", "", 7200)])
        assert first == again
        info = {s["id"]: s for s in store.list_series()}
        # Empty metadata never overwrites known values; retention does update
        assert info["k1"]["node"] == "n"
        assert info["k1"]["retention_s"] == 7200

    def test_query_unknown_series_is_empty(self, store):
        res = store.query(["nope"], 0, time.time(), 500)
        assert res["series"] == {}
        assert res["tier"] == 0

    def test_query_without_db_file(self, tmp_path):
        s = TimeSeriesStore(tmp_path / "missing.db")
        assert s.query(["x"], 0, 10, 100)["series"] == {}
        assert not (tmp_path / "missing.db").exists()

    def test_pick_tier(self):
        assert pick_tier(3600, 1000) == TIERS[0]
        assert pick_tier(7 * DAY, 1000) == 1800
        assert pick_tier(10 * 365 * DAY, 100) == TIERS[-1]


class TestRecorder:
    def test_raw_samples_round_trip(self, store, recorder):
        t0 = 1_700_000_000.0
        for i in range(10):
            recorder.ingest([dp("a_bme_temp", 20.0 + i)], t0 + i * 5)
        assert recorder.flush() == 10
        res = store.query(["a_bme_temp"], t0 - 1, t0 + 100, 500)
        assert res["tier"] == 0
        col = res["series"]["a_bme_temp"]
        assert col["v"] == [20.0 + i for i in range(10)]
        assert col["t"] == [t0 + i * 5 for i in range(10)]

    def test_ignores_unrecorded_and_non_numeric(self, store, recorder):
        t0 = 1_700_000_000.0
        recorder.ingest(
            [dp("other", 1.0), dp("a_bme_temp", "n/a"), dp("a_bme_temp", float("nan")), dp("a_bme_temp", True)],
            t0,
        )
        assert recorder.flush() == 0

    def test_aggregates_match_raw(self, store, recorder):
        t0 = 1_700_000_000 - (1_700_000_000 % 10800)  # aligned to every tier
        values = [float(v) for v in range(1, 121)]  # 120 samples, 5 s apart = 10 min
        for i, v in enumerate(values):
            recorder.ingest([dp("a_bme_temp", v)], t0 + i * 5)
        recorder.flush()

        # First 1-minute bucket holds 12 samples: 1..12
        ts, n, vmin, vmax, vsum = agg_row(store, "a_bme_temp", 60, t0)
        assert (ts, n, vmin, vmax, vsum) == (t0, 12, 1.0, 12.0, sum(range(1, 13)))
        # 5-minute bucket: 60 samples
        _, n, vmin, vmax, vsum = agg_row(store, "a_bme_temp", 300, t0)
        assert (n, vmin, vmax, vsum) == (60, 1.0, 60.0, sum(range(1, 61)))
        # 30-minute bucket still open, holds everything so far
        _, n, vmin, vmax, vsum = agg_row(store, "a_bme_temp", 1800, t0)
        assert (n, vmin, vmax, vsum) == (120, 1.0, 120.0, sum(values))

    def test_open_bucket_survives_restart(self, store, recorder):
        t0 = 1_700_000_000 - (1_700_000_000 % 60)
        for i in range(3):
            recorder.ingest([dp("a_bme_temp", 10.0)], t0 + i)
        recorder.flush()
        # A new recorder (gateway restart) resumes the same bucket from disk
        rec2 = Recorder(store)
        rec2.set_series(store.lookup(["a_bme_temp"]))
        rec2.ingest([dp("a_bme_temp", 40.0)], t0 + 10)
        rec2.flush()
        _, n, vmin, vmax, vsum = agg_row(store, "a_bme_temp", 60, t0)
        assert (n, vmin, vmax, vsum) == (4, 10.0, 40.0, 70.0)

    def test_restart_with_stale_clock_keeps_stored_buckets(self, store, recorder):
        # Boot with a clock behind stored data (no RTC): stepping forward into
        # buckets that already exist must continue them, not overwrite them.
        t0 = 1_700_000_000 - (1_700_000_000 % 10800)
        for i in range(6):  # buckets t0, t0+60, t0+120: 2 samples each
            recorder.ingest([dp("a_bme_temp", 5.0)], t0 + i * 30)
        recorder.flush()
        rec2 = Recorder(store)
        rec2.set_series(store.lookup(["a_bme_temp"]))
        rec2.ingest([dp("a_bme_temp", 1.0)], t0 + 10)
        rec2.ingest([dp("a_bme_temp", 9.0)], t0 + 70)
        rec2.flush()
        assert agg_row(store, "a_bme_temp", 60, t0)[1:] == (3, 1.0, 5.0, 11.0)
        assert agg_row(store, "a_bme_temp", 60, t0 + 60)[1:] == (3, 5.0, 9.0, 19.0)
        assert agg_row(store, "a_bme_temp", 60, t0 + 120)[1:] == (2, 5.0, 5.0, 10.0)

    def test_late_sample_merges_into_closed_bucket(self, store, recorder):
        t0 = 1_700_000_000 - (1_700_000_000 % 60)
        recorder.ingest([dp("a_bme_temp", 1.0)], t0)
        recorder.ingest([dp("a_bme_temp", 2.0)], t0 + 120)  # closes bucket t0
        recorder.flush()
        recorder.ingest([dp("a_bme_temp", 9.0)], t0 + 30)  # clock stepped back
        recorder.flush()
        _, n, vmin, vmax, vsum = agg_row(store, "a_bme_temp", 60, t0)
        assert (n, vmin, vmax, vsum) == (2, 1.0, 9.0, 10.0)

    def test_query_picks_aggregate_tier_when_dense(self, store, recorder):
        t0 = 1_700_000_000 - (1_700_000_000 % 10800)
        for i in range(3000):  # ~4h at 5 s
            recorder.ingest([dp("a_bme_temp", float(i % 50))], t0 + i * 5)
        recorder.flush()
        res = store.query(["a_bme_temp"], t0, t0 + 3000 * 5, points=200)
        assert res["tier"] in TIERS
        col = res["series"]["a_bme_temp"]
        assert len(col["t"]) <= 400
        assert set(col) == {"t", "avg", "min", "max"}
        assert all(lo <= avg <= hi for lo, avg, hi in zip(col["min"], col["avg"], col["max"]))

    def test_incremental_since(self, store, recorder):
        t0 = 1_700_000_000.0
        for i in range(5):
            recorder.ingest([dp("a_bme_temp", float(i))], t0 + i)
        recorder.flush()
        res = store.query(["a_bme_temp"], t0, t0 + 10, 500, tier=0, since_s=t0 + 3)
        assert res["series"]["a_bme_temp"]["v"] == [3.0, 4.0]

    def test_unflushed_samples_are_queryable(self, store, recorder):
        t0 = 1_700_000_000.0
        recorder.ingest([dp("a_bme_temp", 1.0)], t0)
        recorder.flush()
        recorder.ingest([dp("a_bme_temp", 2.0)], t0 + 1)  # still buffered
        res = store.query(["a_bme_temp"], t0 - 1, t0 + 5, 500, unflushed=recorder.unflushed_snapshot())
        assert res["series"]["a_bme_temp"]["v"] == [1.0, 2.0]

    def test_prune_applies_retention(self, store, recorder):
        now = time.time()
        recorder.ingest([dp("a_bme_temp", 1.0)], now - 8 * DAY)
        recorder.ingest([dp("a_bme_temp", 2.0)], now - 60)
        recorder.flush()
        assert store.prune(now) > 0
        res = store.query(["a_bme_temp"], now - 30 * DAY, now, 500)
        assert res["series"]["a_bme_temp"]["v"] == [2.0]

    def test_purge_one_series(self, store, recorder):
        t0 = 1_700_000_000.0
        recorder.ingest([dp("a_bme_temp", 1.0), dp("b_bme_temp", 2.0)], t0)
        recorder.flush()
        recorder.purge(["a_bme_temp"])
        res = store.query(["a_bme_temp", "b_bme_temp"], t0 - 1, t0 + 1, 500)
        assert res["series"]["a_bme_temp"]["v"] == []
        assert res["series"]["b_bme_temp"]["v"] == [2.0]
        # Accumulator was reset: new samples start a clean bucket
        recorder.ingest([dp("a_bme_temp", 5.0)], t0 + 1)
        recorder.flush()
        assert agg_row(store, "a_bme_temp", 60, int(t0) - int(t0) % 60)[1] == 1


class TestExport:
    def test_wide_csv_merges_by_timestamp(self, store, recorder):
        t0 = 1_700_000_000.0
        recorder.ingest([dp("a_bme_temp", 1.5), dp("b_bme_temp", 2.5)], t0)
        recorder.ingest([dp("b_bme_temp", 3.5)], t0 + 10)
        recorder.flush()
        tz, label = resolve_tz(None, 0)
        series = [
            {"id": "a_bme_temp", "node": "a", "name": "Temperature", "units": "C"},
            {"id": "b_bme_temp", "node": "b", "name": "Temperature", "units": "C"},
        ]
        text = b"".join(csv_wide(store, series, t0 - 1, t0 + 20, tz, label)).decode()
        lines = text.strip().split("\n")
        assert lines[0] == "time (UTC+00:00),unix_s,a Temperature (C),b Temperature (C)"
        assert lines[1].endswith(",1700000000.000,1.5,2.5")
        assert lines[2].endswith(",1700000010.000,,3.5")

    def test_long_csv(self, store, recorder):
        t0 = 1_700_000_000.0
        recorder.ingest([dp("a_bme_temp", 1.5)], t0)
        recorder.flush()
        tz, label = resolve_tz("UTC", None)
        series = [{"id": "a_bme_temp", "node": "a", "name": "Temperature", "units": "C"}]
        text = b"".join(csv_long(store, series, t0 - 1, t0 + 1, tz, label)).decode()
        assert text.split("\n")[1] == "2023-11-14 22:13:20.000,1700000000.000,a_bme_temp,a,Temperature,C,1.5"

    def test_safe_filename(self):
        assert safe_filename("Temps / patio!") == "Temps_patio"
        assert safe_filename("") == "export"


def readings(*pairs):
    return [SensorReading(name=n, units=u, value=v, sensor_class="BME280", timestamp=0) for n, u, v in pairs]


class TestService:
    @pytest.fixture
    def collector(self):
        c = SensorDataCollector("gw", None)
        c.add_readings("patio", readings(("Temperature", "C", 20.0), ("Humidity", "%", 50.0)), rssi=-80)
        c.add_readings("shed", readings(("Temperature", "C", 15.0)), rssi=-90)
        return c

    @pytest.fixture
    def service(self, tmp_path, collector):
        svc = LoggerService(tmp_path / "logger", collector=collector, flush_interval=3600)
        yield svc
        svc.stop()

    def test_idle_until_enabled(self, service, collector):
        assert not service.recording
        assert collector._listener is None
        assert not (service.data_dir / "timeseries.db").exists()

    def test_chart_validation(self, service):
        with pytest.raises(ValueError, match="share units"):
            service.create_chart({"series": ["patio_bme280_temperature", "patio_bme280_humidity"]})
        with pytest.raises(ValueError, match="unknown series"):
            service.create_chart({"series": ["missing"]})
        with pytest.raises(ValueError, match="at least one"):
            service.create_chart({"series": []})
        with pytest.raises(ValueError, match="max age"):
            service.create_chart({"series": ["shed_bme280_temperature"], "max_age_s": 5})
        with pytest.raises(NotFound):
            service.update_chart("nope", {})

    def test_colors_follow_series(self, service):
        chart = service.create_chart({"series": ["patio_bme280_temperature", "shed_bme280_temperature"]})
        colors = {s["id"]: s["color"] for s in chart["series"]}
        assert sorted(colors.values()) == [0, 1]
        # Removing the first series keeps the survivor's color
        updated = service.update_chart(chart["id"], {"series": ["shed_bme280_temperature"]})
        assert updated["series"][0]["color"] == colors["shed_bme280_temperature"]
        assert chart["title"] == "Temperature"  # defaults to the first series' name

    def test_records_through_collector(self, service, collector):
        chart = service.create_chart({"series": ["patio_bme280_temperature"], "max_age_s": DAY})
        service.set_enabled(True)
        assert service.recording
        collector.add_readings("patio", readings(("Temperature", "C", 21.0)))
        now = time.time()
        res = service.query(["patio_bme280_temperature"], now - 60, now + 1, 500)
        assert res["series"]["patio_bme280_temperature"]["v"] == [21.0]  # served from the buffer
        service.set_enabled(False)  # final flush
        res = service.query(["patio_bme280_temperature"], now - 60, now + 1, 500)
        assert res["series"]["patio_bme280_temperature"]["v"] == [21.0]  # now from disk
        assert collector._listener is None
        assert chart["id"] in {c["id"] for c in service.state()["charts"]}

    def test_config_persists(self, tmp_path, collector):
        svc = LoggerService(tmp_path / "logger", collector=collector)
        svc.create_chart({"series": ["shed_bme280_temperature"], "max_age_s": 3 * DAY})
        svc.set_enabled(True)
        svc.stop()
        again = LoggerService(tmp_path / "logger", collector=SensorDataCollector("gw", None))
        assert again.enabled
        assert again.state()["charts"][0]["max_age_s"] == 3 * DAY
        # Remembered metadata keeps the series in the catalog after a restart
        assert any(c["id"] == "shed_bme280_temperature" for c in again.state()["catalog"])
