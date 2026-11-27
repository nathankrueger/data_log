"""
Multi-resolution time-series store on SQLite.

Tables
------
series(sid, key, node, name, units, retention_s, created)
    One row per recorded sensor series. ``key`` is the sensor id
    ({node}_{class}_{reading}); ``sid`` is a compact integer used everywhere else.

raw(sid, ts, v)                            WITHOUT ROWID, PRIMARY KEY (sid, ts)
    Every sample. ts is integer milliseconds. The clustered primary key means
    a series' samples are stored contiguously in time order, so range reads,
    "latest N" reads and retention deletes are all index range operations.

agg(sid, tier, ts, n, vmin, vmax, vsum)    WITHOUT ROWID, PRIMARY KEY (sid, tier, ts)
    Pre-aggregated buckets at each width in TIERS (1m/5m/30m/3h). ts is the
    bucket start in seconds. Maintained incrementally by the Recorder.

Complexity
----------
A chart can only show about one point per pixel. query() returns raw samples
when the window holds at most ``2 * points`` of them (checked with a LIMIT, so
the check itself is bounded), otherwise the finest tier whose bucket count
fits. Reads are O(log n + points) no matter how many weeks are stored -
no full scans, no Python-side downsampling. Min/max per bucket lets the UI
draw an envelope so spikes never disappear when zoomed out.

Concurrency
-----------
WAL mode. One long-lived writer connection (owned by the Recorder while
recording is on, guarded by a lock), plus short-lived/pooled reader
connections for HTTP threads. Readers never block the writer.
"""

from __future__ import annotations

import heapq
import logging
import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from gateway.timeseries.constants import (
    AUTO_MIN_SPAN_S,
    DERIVED_TIERS,
    POINTS_MAX,
    POINTS_MIN,
    QUERY_TIERS,
    TIERS,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS series (
        sid         INTEGER PRIMARY KEY,
        key         TEXT    NOT NULL UNIQUE,
        node        TEXT    NOT NULL DEFAULT '',
        name        TEXT    NOT NULL DEFAULT '',
        units       TEXT    NOT NULL DEFAULT '',
        retention_s INTEGER NOT NULL,
        created     REAL    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw (
        sid INTEGER NOT NULL,
        ts  INTEGER NOT NULL,
        v   REAL    NOT NULL,
        PRIMARY KEY (sid, ts)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS agg (
        sid  INTEGER NOT NULL,
        tier INTEGER NOT NULL,
        ts   INTEGER NOT NULL,
        n    INTEGER NOT NULL,
        vmin REAL    NOT NULL,
        vmax REAL    NOT NULL,
        vsum REAL    NOT NULL,
        PRIMARY KEY (sid, tier, ts)
    ) WITHOUT ROWID
    """,
)

_UPSERT_SERIES = """
INSERT INTO series (key, node, name, units, retention_s, created) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (key) DO UPDATE SET
    node  = CASE WHEN excluded.node  != '' THEN excluded.node  ELSE node  END,
    name  = CASE WHEN excluded.name  != '' THEN excluded.name  ELSE name  END,
    units = CASE WHEN excluded.units != '' THEN excluded.units ELSE units END,
    retention_s = excluded.retention_s
"""
_RAW_RANGE = "SELECT ts, v FROM raw WHERE sid = ? AND ts >= ? AND ts <= ? ORDER BY ts"
_AGG_RANGE = (
    "SELECT ts, n, vmin, vmax, vsum FROM agg "
    "WHERE sid = ? AND tier = ? AND ts >= ? AND ts <= ? ORDER BY ts"
)
# A derived tier (see DERIVED_TIERS): sum the stored base-tier buckets inside
# each wider bucket. One index range scan; the base tier divides it evenly.
_DERIVED_RANGE = (
    "SELECT ts - ts % ? AS b, sum(n), min(vmin), max(vmax), sum(vsum) FROM agg "
    "WHERE sid = ? AND tier = ? AND ts >= ? AND ts <= ? GROUP BY b ORDER BY b"
)
_INSERT_RAW = "INSERT OR REPLACE INTO raw (sid, ts, v) VALUES (?, ?, ?)"
_REPLACE_AGG = (
    "INSERT OR REPLACE INTO agg (sid, tier, ts, n, vmin, vmax, vsum) VALUES (?, ?, ?, ?, ?, ?, ?)"
)
# Used for late samples that land in an already-closed bucket
_MERGE_AGG = """
INSERT INTO agg (sid, tier, ts, n, vmin, vmax, vsum) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (sid, tier, ts) DO UPDATE SET
    n    = n + excluded.n,
    vmin = min(vmin, excluded.vmin),
    vmax = max(vmax, excluded.vmax),
    vsum = vsum + excluded.vsum
"""

# Per-series row cap for any single read. Normal reads return ~2x the chart
# width; this only bounds a crafted or buggy request (a 512 MB Pi must not be
# OOM-killed - taking LoRa down with it - by one HTTP request).
_MAX_ROWS = 2 * POINTS_MAX + 2


class StaleWriterError(RuntimeError):
    """A recorder that has been replaced tried to write (see set_owner)."""


# set_owner(NO_WRITER): writes from every recorder are refused (the service
# sets this between stopping one recorder and starting the next). An owner of
# None (never set, e.g. tests) means writes are not fenced at all.
NO_WRITER = object()


# Primary SQLite result codes that mean the file itself is damaged
_SQLITE_CORRUPT = 11
_SQLITE_NOTADB = 26

# Every commit appends whole 4 KB pages to the WAL, and SQLite only folds them
# back into the database after 1000 pages (~4 MB) by default, so a small
# history sat mostly in a log file that never shrank. Checkpoint every ~400 KB
# instead, and cut the file back down after an unusually large transaction.
WAL_AUTOCHECKPOINT_PAGES = 100
WAL_SIZE_LIMIT_BYTES = 1 << 20


def is_corruption(exc: BaseException) -> bool:
    """True if a sqlite3 error means the database file is damaged (not just
    busy, full or unreadable), i.e. retrying can never succeed."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)  # Python 3.11+
    if code is not None:
        return (code & 0xFF) in (_SQLITE_CORRUPT, _SQLITE_NOTADB)
    msg = str(exc).lower()
    return "malformed" in msg or "not a database" in msg


def to_ms(seconds: float) -> int:
    return int(round(seconds * 1000.0))


def pick_tier(span_s: float, budget: int) -> int:
    """Finest aggregate tier (stored or derived) whose bucket count over span_s fits the budget."""
    for tier in QUERY_TIERS:
        if span_s / tier <= budget:
            return tier
    return QUERY_TIERS[-1]


def _sig(x: float) -> float:
    """Trim float noise from averages (7 significant digits) to keep JSON small."""
    return float(f"{x:.7g}")


@contextmanager
def _tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        # SQLite may already have rolled back (I/O error, SQLITE_FULL), and a
        # failed COMMIT (busy) leaves the transaction open: roll back only if
        # one is active, and never let the rollback mask the real error.
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


def _oldest_sample(conn, sids: dict, keys, unflushed) -> float | None:
    """Oldest sample (seconds) of any key: stored rows (index seek per series)
    or samples still in the recorder's write buffer."""
    oldest: int | None = None
    for key in keys:
        sid = sids.get(key)
        if sid is None:
            continue
        row = conn.execute("SELECT ts FROM raw WHERE sid = ? ORDER BY ts LIMIT 1", (sid,)).fetchone()
        if row and (oldest is None or row[0] < oldest):
            oldest = row[0]
        extra = unflushed.get(sid) if unflushed else None
        if extra:
            ts = min(r[0] for r in extra)
            if oldest is None or ts < oldest:
                oldest = ts
    return None if oldest is None else oldest / 1000.0


def _lookup(conn: sqlite3.Connection, keys: Iterable[str]) -> dict[str, int]:
    keys = list(dict.fromkeys(keys))
    out: dict[str, int] = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i : i + 500]
        sql = f"SELECT key, sid FROM series WHERE key IN ({','.join('?' * len(chunk))})"
        out.update(conn.execute(sql, chunk).fetchall())
    return out


def _tagged(cur: sqlite3.Cursor, idx: int) -> Iterator[tuple[int, int, float]]:
    while True:
        rows = cur.fetchmany(1024)
        if not rows:
            return
        for ts, v in rows:
            yield ts, idx, v


class TimeSeriesStore:
    """SQLite-backed multi-resolution store. Thread-safe."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._wlock = threading.RLock()
        self._wconn: sqlite3.Connection | None = None
        self._pool: list[sqlite3.Connection] = []
        self._pool_lock = threading.Lock()
        self._pool_gen = 0  # bumped when the file may have been replaced
        self._owner = None  # the one recorder allowed to write_batch(); None = unfenced
        self._used: int | None = None  # bytes the database holds, measured after each write

    # -------------------------------------------------------------------------
    # Connections
    # -------------------------------------------------------------------------

    def exists(self) -> bool:
        return self.path.exists()

    def size_bytes(self) -> int:
        """Space the files take on disk (database + WAL)."""
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.stat(f"{self.path}{suffix}").st_size
            except OSError:
                pass
        return total

    def used_bytes(self, refresh: bool = False) -> int | None:
        """
        Bytes the database holds (its pages, wherever they currently live, minus
        free pages), as of the last write. None if not measured since start-up
        and refresh is False. refresh=True measures now with a reader.
        """
        if refresh and self.exists():
            try:
                with self._reader() as conn:
                    self._note_size(conn)
            except sqlite3.Error as e:
                logger.debug(f"Store size check failed: {e}")
        return self._used

    def _note_size(self, conn: sqlite3.Connection) -> None:
        # Never raises: it runs right after a commit, and a flush reported as
        # failed would be retried (double-counting merged late samples).
        try:
            (pages,) = conn.execute("PRAGMA page_count").fetchone()
            (free,) = conn.execute("PRAGMA freelist_count").fetchone()
            (page_size,) = conn.execute("PRAGMA page_size").fetchone()
        except sqlite3.Error as e:
            logger.debug(f"Store size check failed: {e}")
            return
        self._used = max(0, pages - free) * page_size

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), timeout=15.0, isolation_level=None, check_same_thread=False
        )
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-1024")  # 1 MiB page cache per connection
        conn.execute(f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_PAGES}")
        conn.execute(f"PRAGMA journal_size_limit={WAL_SIZE_LIMIT_BYTES}").fetchall()
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        if version == 0:
            # auto_vacuum must be chosen before the first table is created
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL").fetchall()
        if version < SCHEMA_VERSION:
            with _tx(conn):
                for stmt in _SCHEMA:
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def open_writer(self) -> None:
        """Open the long-lived writer connection (called when recording starts)."""
        with self._wlock:
            if self._wconn is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            try:
                self._init_schema(conn)
                self._note_size(conn)
            except BaseException:
                conn.close()
                raise
            self._wconn = conn

    def close_writer(self) -> None:
        """Close the writer and pooled readers (called when recording stops)."""
        with self._wlock:
            conn, self._wconn = self._wconn, None
            if conn is not None:
                try:
                    conn.execute("PRAGMA optimize")
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                except sqlite3.Error as e:
                    logger.debug(f"Store close housekeeping failed: {e}")
                conn.close()
        with self._pool_lock:
            pool, self._pool = self._pool, []
            self._pool_gen += 1  # readers checked out now must not be re-pooled
        for c in pool:
            c.close()

    def set_owner(self, owner) -> None:
        """
        Fence writers: only `owner` may write_batch() from now on. A recorder
        that outlived its stop() timeout (or a stray flush from a retired one)
        would otherwise overwrite open buckets with its stale in-memory totals.
        Taking the write lock means any write in progress finishes first.
        """
        with self._wlock:
            self._owner = owner

    def quarantine(self) -> Path | None:
        """
        Move a damaged database (and its WAL/SHM) aside so a fresh one can be
        created. Returns the new path of the main file. Never deletes data:
        the damaged copy stays next to it for manual recovery.
        """
        self.close_writer()
        self._used = None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        moved = None
        for suffix in ("", "-wal", "-shm"):
            src = Path(f"{self.path}{suffix}")
            if src.exists():
                dst = Path(f"{self.path}.corrupt-{stamp}{suffix}")
                os.replace(src, dst)
                if not suffix:
                    moved = dst
        if moved:
            logger.error(f"History database was corrupt; moved it to {moved} and starting a new one")
        return moved

    @contextmanager
    def _writer(self) -> Iterator[sqlite3.Connection]:
        with self._wlock:
            if self._wconn is not None:
                yield self._wconn
                return
            # Not recording: short-lived connection for admin ops (purge etc.)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            try:
                self._init_schema(conn)
                yield conn
            finally:
                conn.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        conn = None
        with self._pool_lock:
            gen = self._pool_gen
            if self._pool:
                conn = self._pool.pop()
        if conn is None:
            conn = self._connect()
            conn.execute("PRAGMA query_only=1")
        broken = False
        try:
            yield conn
        except sqlite3.DatabaseError:
            broken = True
            raise
        finally:
            keep = False
            if not broken:
                with self._pool_lock:
                    # Pool only while recording (when idle, hold no connections
                    # at all) and never across a writer close / quarantine: a
                    # connection opened before then may point at the old file.
                    if self._wconn is not None and gen == self._pool_gen and len(self._pool) < 2:
                        self._pool.append(conn)
                        keep = True
            if not keep:
                conn.close()

    # -------------------------------------------------------------------------
    # Series registry
    # -------------------------------------------------------------------------

    def ensure_series(self, entries: Iterable[tuple[str, str, str, str, int]]) -> dict[str, int]:
        """Upsert (key, node, name, units, retention_s) rows; return key -> sid."""
        entries = list(entries)
        if not entries:
            return {}
        now = time.time()
        with self._writer() as conn:
            with _tx(conn):
                conn.executemany(
                    _UPSERT_SERIES,
                    [(k, node, name, units, int(ret), now) for k, node, name, units, ret in entries],
                )
            return _lookup(conn, [e[0] for e in entries])

    def lookup(self, keys: Iterable[str]) -> dict[str, int]:
        if not self.exists():
            return {}
        with self._reader() as conn:
            return _lookup(conn, keys)

    def list_series(self) -> list[dict]:
        """Every stored series with its time span. O(series * log n)."""
        if not self.exists():
            return []
        with self._reader() as conn:
            try:
                rows = conn.execute(
                    "SELECT sid, key, node, name, units, retention_s FROM series"
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            out = []
            for sid, key, node, name, units, retention in rows:
                first = conn.execute(
                    "SELECT ts FROM raw WHERE sid = ? ORDER BY ts LIMIT 1", (sid,)
                ).fetchone()
                last = conn.execute(
                    "SELECT ts FROM raw WHERE sid = ? ORDER BY ts DESC LIMIT 1", (sid,)
                ).fetchone()
                out.append({
                    "id": key,
                    "node": node,
                    "name": name,
                    "units": units,
                    "retention_s": retention,
                    "first_ts": first[0] / 1000.0 if first else None,
                    "last_ts": last[0] / 1000.0 if last else None,
                })
            return out

    # -------------------------------------------------------------------------
    # Writes (Recorder thread, or admin ops)
    # -------------------------------------------------------------------------

    def load_bucket(self, sid: int, tier: int, ts: int) -> tuple | None:
        """Current state (ts, n, vmin, vmax, vsum) of one bucket, if stored."""
        with self._writer() as conn:
            return conn.execute(
                "SELECT ts, n, vmin, vmax, vsum FROM agg WHERE sid = ? AND tier = ? AND ts = ?",
                (sid, tier, ts),
            ).fetchone()

    def latest_bucket(self, sid: int, tier: int) -> int:
        """Start of the newest stored bucket for (sid, tier), or -1. O(log n)."""
        with self._writer() as conn:
            row = conn.execute(
                "SELECT ts FROM agg WHERE sid = ? AND tier = ? ORDER BY ts DESC LIMIT 1",
                (sid, tier),
            ).fetchone()
        return row[0] if row else -1

    def write_batch(
        self,
        raw_rows: Sequence[tuple[int, int, float]],
        agg_rows: Sequence[tuple],
        merge_rows: Sequence[tuple] = (),
        owner=None,
    ) -> None:
        """Write one flush worth of samples and bucket updates in one transaction."""
        with self._writer() as conn:
            if owner is not None and self._owner is not None and owner is not self._owner:
                raise StaleWriterError("this recorder has been replaced; write refused")
            with _tx(conn):
                if raw_rows:
                    conn.executemany(_INSERT_RAW, raw_rows)
                if agg_rows:
                    conn.executemany(_REPLACE_AGG, agg_rows)
                if merge_rows:
                    conn.executemany(_MERGE_AGG, merge_rows)
            self._note_size(conn)

    def prune(self, now: float | None = None) -> int:
        """Apply each series' retention. Index range deletes: O(log n + deleted)."""
        now = time.time() if now is None else now
        deleted = 0
        with self._writer() as conn:
            series = conn.execute("SELECT sid, retention_s FROM series").fetchall()
            with _tx(conn):
                for sid, retention in series:
                    if retention <= 0:
                        continue  # kept forever
                    cutoff = now - retention
                    deleted += conn.execute(
                        "DELETE FROM raw WHERE sid = ? AND ts < ?", (sid, to_ms(cutoff))
                    ).rowcount
                    for tier in TIERS:
                        # Drop buckets that end at or before the cutoff
                        deleted += conn.execute(
                            "DELETE FROM agg WHERE sid = ? AND tier = ? AND ts <= ?",
                            (sid, tier, int(cutoff) - tier),
                        ).rowcount
            if deleted:
                self._maybe_shrink(conn)
                self._note_size(conn)
        return deleted

    def purge(self, keys: Iterable[str] | None = None) -> list[int]:
        """Delete stored samples (all series when keys is None). Returns the sids purged."""
        with self._writer() as conn:
            with _tx(conn):
                if keys is None:
                    sids = [r[0] for r in conn.execute("SELECT sid FROM series").fetchall()]
                    conn.execute("DELETE FROM raw")
                    conn.execute("DELETE FROM agg")
                else:
                    sids = list(_lookup(conn, keys).values())
                    for sid in sids:
                        conn.execute("DELETE FROM raw WHERE sid = ?", (sid,))
                        conn.execute("DELETE FROM agg WHERE sid = ?", (sid,))
            self._maybe_shrink(conn, force=True)
            self._note_size(conn)
        return sids

    def _maybe_shrink(self, conn: sqlite3.Connection, force: bool = False) -> None:
        """
        Return free pages to the filesystem only when a lot was freed (a purge,
        or retention shortened). In steady state the rolling window's freed
        pages are simply reused by new inserts.
        """
        try:
            (free,) = conn.execute("PRAGMA freelist_count").fetchone()
            (total,) = conn.execute("PRAGMA page_count").fetchone()
            if free and (force or (free > 1024 and free > total * 0.25)):
                conn.execute("PRAGMA incremental_vacuum").fetchall()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.Error as e:
            logger.debug(f"Store shrink skipped: {e}")

    # -------------------------------------------------------------------------
    # Reads (HTTP threads)
    # -------------------------------------------------------------------------

    def query(
        self,
        keys: Sequence[str],
        start_s: float,
        end_s: float,
        points: int,
        tier: int | None = None,
        since_s: float | None = None,
        unflushed: dict[int, list[tuple[int, float]]] | None = None,
        auto: bool = False,
    ) -> dict:
        """
        Columnar data for a chart.

        tier=None lets the store choose (raw if it fits 2*points per series,
        else the finest aggregate tier that fits). Passing the tier back with
        since_s gives cheap incremental updates: only rows at or after since_s.

        auto=True fits the window to the data: start moves up to the oldest
        stored sample (keeping at least AUTO_MIN_SPAN_S), so the resolution is
        picked for the data actually held. O(keys * log n) extra.

        Returns {"tier", "start", "end", "first", "series": {key: columns}}
        where raw columns are {"t", "v"} (t in seconds), aggregate columns are
        {"t", "avg", "min", "max"} (t = bucket start), start is the window
        actually used and first the oldest sample (auto only, else None).
        """
        budget = 2 * max(POINTS_MIN, min(POINTS_MAX, int(points)))
        result: dict = {"tier": 0, "start": start_s, "end": end_s, "first": None, "series": {}}
        if not keys or not self.exists():
            if tier:
                result["tier"] = tier
            return result  # (unflushed samples need the series registry in the DB)

        with self._reader() as conn:
            try:
                sids = _lookup(conn, keys)
            except sqlite3.OperationalError:
                return result  # schema not created yet

            if auto:
                first = _oldest_sample(conn, sids, keys, unflushed)
                if first is not None:
                    result["first"] = first
                    start_s = max(start_s, min(first, end_s - AUTO_MIN_SPAN_S))
                    result["start"] = start_s

            lo_s = start_s if since_s is None else max(start_s, since_s)
            lo_ms = math.floor(lo_s * 1000.0)
            hi_ms = math.ceil(end_s * 1000.0)
            if tier and (end_s - lo_s) / tier > _MAX_ROWS:
                # An explicit tier too fine for this window would return
                # unbounded rows: coarsen it (the client re-syncs on a tier change)
                tier = pick_tier(end_s - lo_s, budget)

            raw: dict[str, list] = {}
            if tier is None:
                chosen = 0
                for key in keys:
                    sid = sids.get(key)
                    if sid is None:
                        continue
                    rows = conn.execute(_RAW_RANGE + " LIMIT ?", (sid, lo_ms, hi_ms, budget + 1)).fetchall()
                    if len(rows) > budget:
                        chosen = pick_tier(end_s - start_s, budget)
                        raw = {}
                        break
                    raw[key] = rows
                tier = chosen
            elif tier == 0:
                for key in keys:
                    sid = sids.get(key)
                    if sid is not None:
                        raw[key] = conn.execute(
                            _RAW_RANGE + " LIMIT ?", (sid, lo_ms, hi_ms, _MAX_ROWS)
                        ).fetchall()

            result["tier"] = tier
            series = result["series"]

            if tier == 0:
                for key in keys:
                    sid = sids.get(key)
                    if sid is None:
                        continue
                    rows = raw.get(key, [])
                    extra = unflushed.get(sid) if unflushed else None
                    if extra:
                        # Samples still in the recorder's write buffer
                        last = rows[-1][0] if rows else lo_ms - 1
                        rows = rows + [r for r in extra if last < r[0] <= hi_ms]
                    series[key] = {
                        "t": [r[0] / 1000.0 for r in rows],
                        "v": [r[1] for r in rows],
                    }
            else:
                if tier not in QUERY_TIERS:
                    raise ValueError(f"unknown tier {tier}")
                b0 = math.floor(lo_s / tier) * tier
                b1 = math.floor(end_s)
                # Even the coarsest tier can't cover a window this long within
                # the row cap: keep the newest buckets rather than the oldest
                b0 = max(b0, (b1 // tier - _MAX_ROWS + 1) * tier)
                for key in keys:
                    sid = sids.get(key)
                    if sid is None:
                        continue
                    if tier in DERIVED_TIERS:
                        rows = conn.execute(
                            _DERIVED_RANGE + " LIMIT ?", (tier, sid, TIERS[-1], b0, b1, _MAX_ROWS)
                        ).fetchall()
                    else:
                        rows = conn.execute(_AGG_RANGE + " LIMIT ?", (sid, tier, b0, b1, _MAX_ROWS)).fetchall()
                    series[key] = {
                        "t": [r[0] for r in rows],
                        "avg": [_sig(r[4] / r[1]) for r in rows],
                        "min": [r[2] for r in rows],
                        "max": [r[3] for r in rows],
                    }
        return result

    def iter_merged(
        self, keys: Sequence[str], start_s: float, end_s: float
    ) -> Iterator[tuple[int, int, float]]:
        """
        Yield (ts_ms, key_index, value) for all keys in time order.

        k-way merge of per-series index scans: O(n log k) time, O(k) memory,
        so exports of any size stream without buffering.
        """
        if not keys or not self.exists():
            return
        lo, hi = math.floor(start_s * 1000.0), math.ceil(end_s * 1000.0)
        with self._reader() as conn:
            sids = _lookup(conn, keys)
            cursors: list[sqlite3.Cursor] = []
            try:
                streams = []
                for idx, key in enumerate(keys):
                    sid = sids.get(key)
                    if sid is None:
                        continue
                    cur = conn.execute(_RAW_RANGE, (sid, lo, hi))
                    cursors.append(cur)
                    streams.append(_tagged(cur, idx))
                yield from heapq.merge(*streams)
            finally:
                for cur in cursors:
                    cur.close()

    def iter_sequential(
        self, keys: Sequence[str], start_s: float, end_s: float
    ) -> Iterator[tuple[int, int, float]]:
        """Yield (ts_ms, key_index, value) one series after another."""
        if not keys or not self.exists():
            return
        lo, hi = math.floor(start_s * 1000.0), math.ceil(end_s * 1000.0)
        with self._reader() as conn:
            sids = _lookup(conn, keys)
            for idx, key in enumerate(keys):
                sid = sids.get(key)
                if sid is None:
                    continue
                cur = conn.execute(_RAW_RANGE, (sid, lo, hi))
                try:
                    yield from _tagged(cur, idx)
                finally:
                    cur.close()
