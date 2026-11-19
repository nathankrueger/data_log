"""
Background recorder: buffers incoming samples and writes them to the store.

Data path
---------
ingest()  (LoRa / local-sensor threads)
    O(readings): dict lookup per reading, append to an in-memory buffer.
    No I/O, so a slow SD card can never stall packet reception.

flush()   (recorder thread, every flush_interval seconds)
    Swaps the buffer out and, in one SQLite transaction, inserts the raw
    samples and the touched aggregate buckets. Bucket state lives in small
    in-memory accumulators (one per series per tier), so each sample costs
    O(len(TIERS)) arithmetic - aggregates never re-scan raw data.

prune()   (recorder thread, every prune_interval seconds)
    Applies each series' retention window.

Batching every ~30 s keeps SD-card write amplification low (one WAL commit
per flush instead of one per packet). Samples not yet flushed are still
served to charts via unflushed_snapshot(), so live views are not delayed.
"""

from __future__ import annotations

import logging
import math
import threading
import time

from gateway.timeseries.constants import TIERS
from gateway.timeseries.store import TimeSeriesStore, is_corruption

logger = logging.getLogger(__name__)

# Hard cap on buffered samples if the disk stalls (oldest are dropped).
# ~6 MB; covers hours of a typical setup's samples.
MAX_PENDING = 50_000

# Wall-clock steps. Retention pruning trusts the wall clock, so a bogus jump
# forward (bad NTP, `date -s`) would irreversibly delete history. After a
# forward step bigger than CLOCK_STEP_S, pruning is held for PRUNE_HOLD_S
# (a legitimate correction, e.g. NTP fixing a stale boot clock, just means
# data is kept a little longer than its retention).
CLOCK_STEP_S = 300.0
PRUNE_HOLD_S = 24 * 3600.0


class Recorder(threading.Thread):
    def __init__(
        self,
        store: TimeSeriesStore,
        flush_interval: float = 30.0,
        prune_interval: float = 300.0,
    ):
        super().__init__(daemon=True, name="HistoryRecorder")
        self._store = store
        self._flush_interval = max(1.0, float(flush_interval))
        self._prune_interval = max(10.0, float(prune_interval))

        # series key -> sid; replaced wholesale (atomic reference swap)
        self._map: dict[str, int] = {}

        self._lock = threading.Lock()  # guards _pending / _inflight
        self._pending: list[tuple[int, int, float]] = []
        self._inflight: list[tuple[int, int, float]] = []

        # Serializes flush / prune / purge; owns the accumulators
        self._work_lock = threading.Lock()
        # (sid, tier) -> [bucket_ts, n, vmin, vmax, vsum]
        self._acc: dict[tuple[int, int], list] = {}
        # (sid, tier) -> newest bucket that may exist on disk. Stepping into a
        # bucket at or below this (e.g. the Pi booted with a stale clock, no
        # RTC) must resume it from disk rather than overwrite it.
        self._high: dict[tuple[int, int], int] = {}

        self._wake = threading.Event()
        self._stopping = threading.Event()

        # Stats for the UI
        self.samples_written = 0
        self.samples_dropped = 0
        self.last_flush = 0.0
        self.last_prune = 0.0
        self.last_error: str | None = None
        self.corrupt = False  # set when the DB file is damaged; the service recovers
        self.consecutive_failures = 0  # flushes failing in a row (watchdog input)
        self._clock_ref = (time.time(), time.monotonic())
        self._prune_hold_until = 0.0

    # -------------------------------------------------------------------------
    # Producer side
    # -------------------------------------------------------------------------

    def set_series(self, mapping: dict[str, int]) -> None:
        """Set which series keys are recorded (key -> sid)."""
        self._map = dict(mapping)

    @property
    def recorded_keys(self) -> list[str]:
        return list(self._map)

    def sids_for(self, keys) -> list[int]:
        m = self._map
        return [m[k] for k in keys if k in m]

    def ingest(self, datapoints: list[dict], receive_time: float) -> None:
        """Collector listener: buffer numeric samples of recorded series."""
        m = self._map
        if not m:
            return
        ts = int(receive_time * 1000)
        rows = []
        for dp in datapoints:
            sid = m.get(dp["id"])
            if sid is None:
                continue
            v = dp["value"]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            v = float(v)
            if not math.isfinite(v):
                continue
            rows.append((sid, ts, v))
        if not rows:
            return
        with self._lock:
            self._pending.extend(rows)
            overflow = len(self._pending) - MAX_PENDING
            if overflow > 0:
                del self._pending[:overflow]
                self.samples_dropped += overflow

    def unflushed_snapshot(self, sids=None) -> dict[int, list[tuple[int, float]]]:
        """Samples not yet committed, grouped by sid (time order). With `sids`,
        only those series (a chart poll shouldn't copy the whole buffer)."""
        wanted = None if sids is None else set(sids)
        with self._lock:
            if wanted is None:
                rows = self._inflight + self._pending
            else:
                rows = [r for r in self._inflight if r[0] in wanted]
                rows += [r for r in self._pending if r[0] in wanted]
        out: dict[int, list[tuple[int, float]]] = {}
        for sid, ts, v in rows:
            out.setdefault(sid, []).append((ts, v))
        return out

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending) + len(self._inflight)

    # -------------------------------------------------------------------------
    # Thread
    # -------------------------------------------------------------------------

    def run(self) -> None:
        logger.info(
            f"History recorder started (flush every {self._flush_interval:.0f}s, "
            f"prune every {self._prune_interval:.0f}s)"
        )
        next_prune = time.monotonic() + 5.0
        while not self._stopping.is_set():
            self._wake.wait(self._flush_interval)
            self._wake.clear()
            self._guard(self.flush)
            if time.monotonic() >= next_prune:
                self._guard(self.prune)
                next_prune = time.monotonic() + self._prune_interval
        self._guard(self.flush)
        logger.info("History recorder stopped")

    def stop(self, timeout: float = 60.0) -> None:
        self._stopping.set()
        self._wake.set()
        if self.is_alive():
            self.join(timeout=timeout)

    def _guard(self, fn) -> None:
        # last_error is cleared only by a successful flush (inside flush()),
        # so a passing prune can't hide a write path that keeps failing.
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - keep recording through transient errors
            self.last_error = str(e)
            if is_corruption(e):
                self.corrupt = True
            logger.error(f"History recorder {fn.__name__} failed: {e}")

    # -------------------------------------------------------------------------
    # Work (all under _work_lock; safe to call from any thread)
    # -------------------------------------------------------------------------

    def flush(self) -> int:
        """Commit buffered samples. Returns the number written."""
        with self._work_lock:
            with self._lock:
                batch, self._pending = self._pending, []
                self._inflight = batch
            if not batch:
                return 0
            try:
                self._drop_stale_accumulators()
                agg_rows, merge_rows = self._aggregate(batch)
                self._store.write_batch(batch, agg_rows, merge_rows, owner=self)
            except Exception as e:
                # The transaction rolled back, so the DB holds the pre-batch
                # state. Forget the accumulators this batch touched (they will
                # be reloaded from the DB) and requeue the batch for retry.
                self.consecutive_failures += 1
                self.last_error = str(e)
                if is_corruption(e):
                    self.corrupt = True
                for sid in {row[0] for row in batch}:
                    for tier in TIERS:
                        self._acc.pop((sid, tier), None)
                        self._high.pop((sid, tier), None)
                with self._lock:
                    # Requeue and drop the in-flight copy atomically, or a
                    # concurrent snapshot would see the batch twice
                    self._inflight = []
                    self._pending[:0] = batch
                    overflow = len(self._pending) - MAX_PENDING
                    if overflow > 0:  # disk stuck: keep the newest samples
                        del self._pending[:overflow]
                        self.samples_dropped += overflow
                raise
            with self._lock:
                self._inflight = []
            self.consecutive_failures = 0
            self.last_error = None
            self.samples_written += len(batch)
            self.last_flush = time.time()
            return len(batch)

    def prune(self) -> int:
        wall, mono = time.time(), time.monotonic()
        step = (wall - self._clock_ref[0]) - (mono - self._clock_ref[1])
        if abs(step) > CLOCK_STEP_S:
            self._clock_ref = (wall, mono)
            if step > 0:
                self._prune_hold_until = mono + PRUNE_HOLD_S
                logger.warning(
                    f"Wall clock jumped forward {step / 3600:.1f} h; holding history pruning "
                    f"for {PRUNE_HOLD_S / 3600:.0f} h in case the new time is wrong"
                )
        if mono < self._prune_hold_until:
            return 0
        with self._work_lock:
            deleted = self._store.prune()
            self.last_prune = time.time()
        if deleted:
            logger.debug(f"History prune removed {deleted} rows")
        return deleted

    def purge(self, keys: list[str] | None) -> list[int]:
        """Delete stored and buffered samples for keys (all when None)."""
        with self._work_lock:
            sids = set(self._store.purge(keys))
            if keys is None:
                self._acc.clear()
                self._high.clear()
            else:
                for key in list(self._acc):
                    if key[0] in sids:
                        del self._acc[key]
                for key in list(self._high):
                    if key[0] in sids:
                        del self._high[key]
            with self._lock:
                if keys is None:
                    self._pending = []
                else:
                    self._pending = [r for r in self._pending if r[0] not in sids]
        return sorted(sids)

    def _drop_stale_accumulators(self) -> None:
        live = set(self._map.values())
        for key in [k for k in self._acc if k[0] not in live]:
            del self._acc[key]
            self._high.pop(key, None)

    def _resume(self, sid: int, tier: int, bucket: int, v: float) -> list:
        """Accumulator for `bucket`, continuing its on-disk state if any."""
        row = self._store.load_bucket(sid, tier, bucket)
        return list(row) if row else [bucket, 0, v, v, 0.0]

    def _aggregate(self, batch: list[tuple[int, int, float]]) -> tuple[list[tuple], list[tuple]]:
        """
        Fold samples into per-(series, tier) accumulators.

        Returns rows for the store:
          agg_rows   full bucket state (INSERT OR REPLACE): closed buckets plus
                     the current open bucket of every touched accumulator
          merge_rows single-sample upserts for late samples that land in an
                     already-closed bucket (clock stepped backwards)
        """
        closed: list[tuple] = []
        merges: list[tuple] = []
        dirty: set[tuple[int, int]] = set()
        acc_map = self._acc
        high = self._high

        for sid, ts_ms, v in batch:
            ts_s = ts_ms // 1000
            for tier in TIERS:
                bucket = ts_s - ts_s % tier
                key = (sid, tier)
                acc = acc_map.get(key)
                if acc is None:
                    # First sample since start: resume the bucket from disk so a
                    # restart doesn't reset its partial aggregate
                    acc = acc_map[key] = self._resume(sid, tier, bucket, v)
                    high[key] = max(self._store.latest_bucket(sid, tier), bucket)

                if bucket > acc[0]:
                    if acc[1]:
                        closed.append((sid, tier, acc[0], acc[1], acc[2], acc[3], acc[4]))
                    if bucket <= high[key]:
                        # Re-entering stored history (clock behind the data):
                        # continue that bucket instead of overwriting it
                        acc[:] = self._resume(sid, tier, bucket, v)
                    else:
                        acc[:] = [bucket, 0, v, v, 0.0]
                        high[key] = bucket

                if bucket == acc[0]:
                    acc[1] += 1
                    if v < acc[2]:
                        acc[2] = v
                    if v > acc[3]:
                        acc[3] = v
                    acc[4] += v
                    dirty.add(key)
                else:
                    merges.append((sid, tier, bucket, 1, v, v, v))

        agg_rows = closed
        for sid, tier in dirty:
            acc = acc_map[(sid, tier)]
            agg_rows.append((sid, tier, acc[0], acc[1], acc[2], acc[3], acc[4]))
        return agg_rows, merges
