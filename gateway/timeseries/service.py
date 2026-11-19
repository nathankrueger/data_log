"""
History logger service: chart configuration, recording lifecycle, queries.

Model
-----
The user builds *charts*. Each chart has a title, 1-8 series that share units,
and a max age. A series is recorded while it appears in at least one chart;
its retention is the longest max age among those charts. Recording is a
single on/off switch that persists across restarts.

Footprint
---------
Nothing heavy happens until recording is turned on: this module does not
import sqlite3, no thread runs, and the collector's listener hook is None.
Viewing stored charts with recording off opens short-lived read connections
only while a request is being served.

State lives in <data_dir>/logger.json (config) and <data_dir>/timeseries.db.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator

from gateway.timeseries.constants import (
    MAX_CHARTS,
    MAX_SERIES_PER_CHART,
    POINTS_MAX,
    POINTS_MIN,
    RETENTION_DEFAULT_S,
    RETENTION_MAX_S,
    RETENTION_MIN_S,
    TIERS,
)

logger = logging.getLogger(__name__)

CONFIG_NAME = "logger.json"
DB_NAME = "timeseries.db"
CONFIG_VERSION = 1

# Watchdog restart backoff for a recorder that died or hit a damaged database
RESTART_BACKOFF_MIN_S = 30.0
RESTART_BACKOFF_MAX_S = 900.0
# A recorder that is alive but whose flushes keep failing (SD I/O errors,
# read-only remount, disk full) gets reopened after this many in a row
FLUSH_FAILURES_BEFORE_RESTART = 5


class NotFound(LookupError):
    pass


def _clean_chart(raw) -> dict | None:
    """Validate/repair one chart from logger.json; None if unusable."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    series: list[dict] = []
    used: set[int] = set()
    for item in raw.get("series") if isinstance(raw.get("series"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            continue
        if any(s["id"] == item["id"] for s in series) or len(series) >= MAX_SERIES_PER_CHART:
            continue
        color = item.get("color")
        ok = isinstance(color, int) and not isinstance(color, bool) and 0 <= color < MAX_SERIES_PER_CHART
        if ok and color not in used:
            used.add(color)
        else:
            color = None
        series.append({"id": item["id"], "color": color})
    if not series:
        return None
    for s in series:
        if s["color"] is None:
            s["color"] = next(i for i in range(MAX_SERIES_PER_CHART) if i not in used)
            used.add(s["color"])
    try:
        max_age = int(raw.get("max_age_s", RETENTION_DEFAULT_S))
    except (TypeError, ValueError):
        max_age = RETENTION_DEFAULT_S
    created = raw.get("created")
    return {
        "id": raw["id"],
        "title": str(raw.get("title") or "")[:80] or series[0]["id"],
        "units": str(raw.get("units") or ""),
        "max_age_s": min(RETENTION_MAX_S, max(RETENTION_MIN_S, max_age)),
        "series": series,
        "created": created if isinstance(created, (int, float)) and not isinstance(created, bool) else time.time(),
    }


class LoggerService:
    def __init__(
        self,
        data_dir: Path | str,
        collector=None,
        flush_interval: float = 30.0,
        prune_interval: float = 300.0,
    ):
        self.data_dir = Path(data_dir)
        self._config_path = self.data_dir / CONFIG_NAME
        self._db_path = self.data_dir / DB_NAME
        self._collector = collector
        self._flush_interval = flush_interval
        self._prune_interval = prune_interval
        self._lock = threading.RLock()
        self._store = None  # TimeSeriesStore, created on first use
        self._recorder = None  # Recorder thread while recording
        self._next_restart = 0.0
        self._restart_backoff = RESTART_BACKOFF_MIN_S
        self._read_corrupt = False  # a reader hit a damaged DB; watchdog recovers
        self._cfg = self._load()

    # -------------------------------------------------------------------------
    # Config persistence
    # -------------------------------------------------------------------------

    def _load(self) -> dict:
        """
        Load logger.json defensively: anything malformed is dropped or
        repaired rather than allowed to raise later, and an unparseable file
        is moved aside (not overwritten) so chart setups can be recovered.
        """
        cfg = {"version": CONFIG_VERSION, "enabled": False, "charts": [], "series_meta": {}}
        try:
            with open(self._config_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return cfg
        except OSError as e:
            logger.error(f"Could not read {self._config_path}: {e} (starting with empty config)")
            return cfg
        except ValueError as e:
            logger.error(f"{self._config_path} is not valid JSON: {e}")
            self._quarantine_config()
            return cfg
        if not isinstance(data, dict):
            logger.error(f"{self._config_path} does not hold an object")
            self._quarantine_config()
            return cfg

        cfg["enabled"] = data.get("enabled") is True
        charts = data.get("charts")
        seen: set[str] = set()
        for raw in charts if isinstance(charts, list) else []:
            chart = _clean_chart(raw)
            if chart is not None and chart["id"] not in seen and len(cfg["charts"]) < MAX_CHARTS:
                seen.add(chart["id"])
                cfg["charts"].append(chart)
        meta = data.get("series_meta")
        if isinstance(meta, dict):
            cfg["series_meta"] = {
                k: v for k, v in meta.items() if isinstance(k, str) and isinstance(v, dict)
            }
        return cfg

    def _quarantine_config(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = self._config_path.with_name(f"{CONFIG_NAME}.corrupt-{stamp}")
        try:
            os.replace(self._config_path, dst)
            logger.error(f"Moved unreadable logger config to {dst}; starting with an empty one")
        except OSError as e:
            logger.error(f"Could not move aside {self._config_path}: {e}")

    def _save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".logger-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._cfg, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())  # survive a power cut right after saving
            os.replace(tmp, self._config_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def _get_store(self):
        # Under the lock: two HTTP threads racing here must not create two
        # store objects (the writer would live on one, quarantine on the other)
        with self._lock:
            if self._store is None:
                from gateway.timeseries.store import TimeSeriesStore

                self._store = TimeSeriesStore(self._db_path)
            return self._store

    def _note_read_error(self, exc: BaseException) -> None:
        from gateway.timeseries.store import is_corruption

        if is_corruption(exc):
            self._read_corrupt = True

    def start(self) -> None:
        """Resume recording if it was on when the gateway last stopped."""
        with self._lock:
            if self._cfg["enabled"]:
                try:
                    self._start_recorder()
                except Exception as e:  # noqa: BLE001 - the watchdog retries
                    logger.error(f"Could not start history recorder: {e}")

    def check_health(self) -> None:
        """
        Watchdog, called periodically by the gateway main loop. If recording
        is switched on but the recorder thread is gone, or its database turned
        out to be damaged, restart it (moving a damaged file aside), with
        exponential backoff so a persistent fault doesn't spin.
        """
        with self._lock:
            rec = self._recorder
            corrupt = self._read_corrupt or (rec is not None and rec.corrupt)
            if not self._cfg["enabled"]:
                if corrupt and self._store is not None:
                    self._read_corrupt = False
                    self._store.quarantine()  # not recording: just set it aside
                return
            failing = rec is not None and rec.consecutive_failures >= FLUSH_FAILURES_BEFORE_RESTART
            if rec is not None and rec.is_alive() and not corrupt and not failing:
                self._restart_backoff = RESTART_BACKOFF_MIN_S
                return
            now = time.monotonic()
            if now < self._next_restart:
                return
            if corrupt:
                problem = "hit a damaged database"
            elif failing:
                problem = f"failed {rec.consecutive_failures} writes in a row ({rec.last_error})"
            else:
                problem = "is not running"
            logger.warning(f"History recorder {problem}; restarting it")
            try:
                self._stop_recorder()
                if corrupt:
                    self._read_corrupt = False
                    self._get_store().quarantine()
                self._start_recorder()
            except Exception as e:  # noqa: BLE001
                logger.error(f"History recorder restart failed: {e}")
            self._next_restart = now + self._restart_backoff
            self._restart_backoff = min(self._restart_backoff * 2, RESTART_BACKOFF_MAX_S)

    def stop(self) -> None:
        with self._lock:
            self._stop_recorder()

    @property
    def enabled(self) -> bool:
        return bool(self._cfg["enabled"])

    @property
    def recording(self) -> bool:
        rec = self._recorder
        return rec is not None and rec.is_alive()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            if enabled:
                if not self.recording:
                    self._stop_recorder()  # clear out a recorder that died
                    self._start_recorder()
            else:
                self._stop_recorder()
            self._cfg["enabled"] = bool(enabled)
            self._save()
        logger.info(f"History recording {'enabled' if enabled else 'disabled'}")

    def _start_recorder(self) -> None:
        if self._recorder is not None:
            return
        from gateway.timeseries.recorder import Recorder
        from gateway.timeseries.store import is_corruption

        store = self._get_store()
        for attempt in (1, 2):
            recorder = Recorder(store, self._flush_interval, self._prune_interval)
            self._recorder = recorder
            try:
                store.open_writer()
                self._sync_series()
                break
            except Exception as e:
                self._recorder = None
                store.close_writer()
                if attempt == 1 and is_corruption(e):
                    store.quarantine()  # start over with a fresh database
                    continue
                raise
        store.set_owner(recorder)  # fence out any straggler from a previous recorder
        recorder.start()
        if self._collector is not None:
            self._collector.set_listener(recorder.ingest)

    def _stop_recorder(self) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        if self._collector is not None:
            self._collector.set_listener(None)
        recorder.stop()  # final flush; waits up to a minute for a slow disk
        if recorder.is_alive():
            logger.error("History recorder did not stop in time; fencing out its writes")
        self._recorder = None
        if self._store is not None:
            from gateway.timeseries.store import NO_WRITER

            # Waits for any write in progress, then refuses the old recorder
            self._store.set_owner(NO_WRITER)
            self._store.close_writer()

    # -------------------------------------------------------------------------
    # Series bookkeeping
    # -------------------------------------------------------------------------

    def _retention_map(self) -> dict[str, int]:
        """Recorded series -> retention (longest max age of charts using it)."""
        out: dict[str, int] = {}
        for chart in self._cfg["charts"]:
            age = int(chart.get("max_age_s", RETENTION_DEFAULT_S))
            for s in chart.get("series", []):
                key = s.get("id")
                if key and age > out.get(key, 0):
                    out[key] = age
        return out

    def _sync_series(self) -> None:
        """Push the recorded set and retentions to the store and recorder."""
        if self._recorder is None:
            return
        retention = self._retention_map()
        meta = self._cfg["series_meta"]
        entries = []
        for key, ret in retention.items():
            m = meta.get(key, {})
            entries.append((key, m.get("node", ""), m.get("name", ""), m.get("units", ""), ret))
        sids = self._get_store().ensure_series(entries)
        self._recorder.set_series(sids)

    def _catalog(self) -> dict[str, dict]:
        """Live series (seen since boot) merged with remembered chart series."""
        out: dict[str, dict] = {}
        if self._collector is not None:
            for s in self._collector.get_series_catalog():
                s["live"] = True
                out[s["id"]] = s
        for key, m in self._cfg["series_meta"].items():
            if key not in out:
                out[key] = {
                    "id": key,
                    "node": m.get("node", ""),
                    "name": m.get("name", key),
                    "units": m.get("units", ""),
                    "local": m.get("local", False),
                    "value": None,
                    "ts": None,
                    "interval_s": m.get("interval_s"),
                    "live": False,
                }
        return out

    # -------------------------------------------------------------------------
    # State for the UI
    # -------------------------------------------------------------------------

    def summary(self) -> dict:
        """Cheap status for the top bar (no database access)."""
        retention = self._retention_map()
        rec = self._recorder
        return {
            "enabled": self.enabled,
            "recording": self.recording,
            "error": rec.last_error if rec is not None else None,
            "series": len(retention),
            "charts": len(self._cfg["charts"]),
            "bytes": self._db_size(),
        }

    def _db_size(self) -> int:
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.stat(f"{self._db_path}{suffix}").st_size
            except OSError:
                pass
        return total

    def state(self) -> dict:
        with self._lock:
            catalog = self._catalog()
            retention = self._retention_map()
            for key, entry in catalog.items():
                entry["retention_s"] = retention.get(key)
            rec = self._recorder
            return {
                "now": time.time(),
                "enabled": self.enabled,
                "recording": self.recording,
                "charts": self._cfg["charts"],
                "catalog": sorted(
                    catalog.values(),
                    key=lambda s: (not s.get("local"), str(s["node"]).lower(), str(s["name"]).lower()),
                ),
                "storage": {
                    "bytes": self._db_size(),
                    "path": str(self._db_path),
                    "series": len(retention),
                },
                "recorder": {
                    "flush_interval_s": self._flush_interval,
                    "last_flush": rec.last_flush if rec else None,
                    "samples_written": rec.samples_written if rec else 0,
                    "pending": rec.pending_count if rec else 0,
                    "dropped": rec.samples_dropped if rec else 0,
                    "last_error": rec.last_error if rec else None,
                    "failures": rec.consecutive_failures if rec else 0,
                },
                "limits": {
                    "max_series_per_chart": MAX_SERIES_PER_CHART,
                    "max_charts": MAX_CHARTS,
                    "retention_min_s": RETENTION_MIN_S,
                    "retention_max_s": RETENTION_MAX_S,
                },
                "tiers": list(TIERS),
            }

    # -------------------------------------------------------------------------
    # Charts
    # -------------------------------------------------------------------------

    def _find(self, chart_id: str) -> dict:
        for chart in self._cfg["charts"]:
            if chart.get("id") == chart_id:
                return chart
        raise NotFound(f"no chart with id '{chart_id}'")

    def _normalize(self, spec: dict, existing: dict | None) -> dict:
        if not isinstance(spec, dict):
            raise ValueError("chart must be an object")
        catalog = self._catalog()

        raw_series = spec.get("series", existing["series"] if existing else [])
        if not isinstance(raw_series, list):
            raise ValueError("'series' must be a list")
        ids: list[str] = []
        requested_colors: dict[str, int] = {}
        for item in raw_series:
            sid = item.get("id") if isinstance(item, dict) else item
            if not isinstance(sid, str) or not sid:
                raise ValueError("each series must be a series id")
            if sid not in ids:
                ids.append(sid)
            if isinstance(item, dict) and isinstance(item.get("color"), int):
                requested_colors[sid] = item["color"]
        if not ids:
            raise ValueError("pick at least one series")
        if len(ids) > MAX_SERIES_PER_CHART:
            raise ValueError(f"a chart holds at most {MAX_SERIES_PER_CHART} series")

        unknown = [i for i in ids if i not in catalog]
        if unknown:
            raise ValueError(f"unknown series: {', '.join(unknown)}")
        units = {catalog[i]["units"] for i in ids}
        if len(units) > 1:
            raise ValueError(f"series in one chart must share units (got {', '.join(sorted(units))})")

        try:
            max_age = int(spec.get("max_age_s", existing["max_age_s"] if existing else RETENTION_DEFAULT_S))
        except (TypeError, ValueError):
            raise ValueError("'max_age_s' must be a number of seconds") from None
        if not RETENTION_MIN_S <= max_age <= RETENTION_MAX_S:
            raise ValueError(
                f"max age must be between {RETENTION_MIN_S // 60} minutes and {RETENTION_MAX_S // 86400} days"
            )

        title = spec.get("title", existing["title"] if existing else "")
        title = str(title or "").strip()[:80] or catalog[ids[0]]["name"]

        # Color follows the series: keep existing assignments, honour requests,
        # then hand out the lowest free palette slot.
        prior = {s["id"]: s.get("color") for s in (existing or {}).get("series", [])}
        used: set[int] = set()
        colors: dict[str, int] = {}
        for sid in ids:
            for candidate in (prior.get(sid), requested_colors.get(sid)):
                if isinstance(candidate, int) and 0 <= candidate < MAX_SERIES_PER_CHART and candidate not in used:
                    colors[sid] = candidate
                    used.add(candidate)
                    break
        for sid in ids:
            if sid not in colors:
                slot = next(i for i in range(MAX_SERIES_PER_CHART) if i not in used)
                colors[sid] = slot
                used.add(slot)

        # Remember metadata so the chart still renders after a restart,
        # before its nodes transmit again
        for sid in ids:
            c = catalog[sid]
            self._cfg["series_meta"][sid] = {
                "node": c.get("node", ""),
                "name": c.get("name", sid),
                "units": c.get("units", ""),
                "local": bool(c.get("local", False)),
                "interval_s": c.get("interval_s"),
            }

        return {
            "id": existing["id"] if existing else secrets.token_hex(4),
            "title": title,
            "units": units.pop(),
            "max_age_s": max_age,
            "series": [{"id": sid, "color": colors[sid]} for sid in ids],
            "created": existing.get("created", time.time()) if existing else time.time(),
        }

    def create_chart(self, spec: dict) -> dict:
        with self._lock:
            if len(self._cfg["charts"]) >= MAX_CHARTS:
                raise ValueError(f"at most {MAX_CHARTS} charts")
            chart = self._normalize(spec, None)
            self._cfg["charts"].append(chart)
            self._commit_charts()
            return chart

    def update_chart(self, chart_id: str, spec: dict) -> dict:
        with self._lock:
            existing = self._find(chart_id)
            chart = self._normalize(spec, existing)
            idx = self._cfg["charts"].index(existing)
            self._cfg["charts"][idx] = chart
            self._commit_charts()
            return chart

    def delete_chart(self, chart_id: str) -> None:
        with self._lock:
            chart = self._find(chart_id)
            self._cfg["charts"].remove(chart)
            self._commit_charts()

    def reorder(self, ids: list[str]) -> None:
        with self._lock:
            by_id = {c["id"]: c for c in self._cfg["charts"]}
            ordered = [by_id.pop(i) for i in ids if i in by_id]
            self._cfg["charts"] = ordered + list(by_id.values())
            self._save()

    def _commit_charts(self) -> None:
        self._prune_series_meta()
        self._save()
        self._sync_series()

    def _prune_series_meta(self) -> None:
        used = {s["id"] for c in self._cfg["charts"] for s in c.get("series", [])}
        meta = self._cfg["series_meta"]
        for key in [k for k in meta if k not in used]:
            del meta[key]

    # -------------------------------------------------------------------------
    # Data
    # -------------------------------------------------------------------------

    def query(
        self,
        keys: list[str],
        start_s: float,
        end_s: float,
        points: int,
        tier: int | None = None,
        since_s: float | None = None,
    ) -> dict:
        if tier is not None and tier != 0 and tier not in TIERS:
            raise ValueError(f"tier must be 0 or one of {list(TIERS)}")
        points = max(POINTS_MIN, min(POINTS_MAX, points))
        rec = self._recorder
        # Snapshot unflushed samples *before* reading the DB: anything flushed
        # in between shows up in both and is de-duplicated by timestamp.
        unflushed = rec.unflushed_snapshot(rec.sids_for(keys)) if rec is not None else None
        store = self._get_store()
        try:
            result = store.query(keys, start_s, end_s, points, tier=tier, since_s=since_s, unflushed=unflushed)
        except Exception as e:
            self._note_read_error(e)
            raise
        result["now"] = time.time()
        return result

    def export_csv(
        self,
        keys: list[str] | None,
        start_s: float,
        end_s: float,
        tz_name: str | None,
        tz_offset_min: int | None,
        layout: str = "wide",
    ) -> Iterator[bytes]:
        from gateway.timeseries.export import csv_long, csv_wide, resolve_tz

        store = self._get_store()
        rec = self._recorder
        if rec is not None:
            try:
                rec.flush()  # include samples still in the write buffer
            except Exception as e:  # noqa: BLE001 - export what is on disk
                logger.warning(f"Pre-export flush failed: {e}")

        stored = {s["id"]: s for s in store.list_series()} if keys is None else {}
        with self._lock:
            catalog = self._catalog()
        if keys is None:
            keys = list(stored)
        series = []
        for key in keys:
            meta = catalog.get(key) or stored.get(key) or {"id": key}
            series.append({
                "id": key,
                "node": meta.get("node", ""),
                "name": meta.get("name", key),
                "units": meta.get("units", ""),
            })
        tz, tz_label = resolve_tz(tz_name, tz_offset_min)
        if layout == "long":
            return csv_long(store, series, start_s, end_s, tz, tz_label)
        return csv_wide(store, series, start_s, end_s, tz, tz_label)

    def storage(self) -> dict:
        with self._lock:
            retention = self._retention_map()
            usage: dict[str, list[str]] = {}
            for chart in self._cfg["charts"]:
                for s in chart.get("series", []):
                    usage.setdefault(s["id"], []).append(chart["title"])
        store = self._get_store()
        try:
            rows = store.list_series()
        except Exception as e:
            self._note_read_error(e)
            raise
        for row in rows:
            row["charts"] = usage.get(row["id"], [])
            row["recording"] = row["id"] in retention and self.recording
        rows.sort(key=lambda r: (str(r["node"]).lower(), str(r["name"]).lower()))
        return {"bytes": self._db_size(), "path": str(self._db_path), "series": rows}

    def purge(self, keys: list[str] | None) -> list[int]:
        """Delete stored samples for keys (all series when None)."""
        with self._lock:
            rec = self._recorder
            if rec is not None:
                return rec.purge(keys)
            store = self._get_store()
            if not store.exists():
                return []
            return store.purge(keys)
