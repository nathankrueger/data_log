"""
CSV export, streamed.

Two layouts:
  wide  one row per timestamp, one column per series (spreadsheet friendly).
        Readings from the same packet share a timestamp, so they land on one
        row; series from different nodes interleave with blanks.
  long  one row per sample: time, unix_s, series, node, sensor, units, value.

Rows are produced from store iterators (k-way merge, constant memory) and
yielded in ~64 KB chunks, so exports of any size stream without buffering.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Iterator, Sequence

from gateway.timeseries.store import TimeSeriesStore

_CHUNK_BYTES = 64 * 1024


def resolve_tz(name: str | None, offset_min: int | None) -> tuple[tzinfo, str]:
    """Browser time zone (IANA name, DST-correct) or a fixed offset fallback."""
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name), name
        except Exception:  # noqa: BLE001 - unknown/invalid zone or no tzdata
            pass
    if offset_min is not None:
        sign = "+" if offset_min >= 0 else "-"
        hh, mm = divmod(abs(int(offset_min)), 60)
        return timezone(timedelta(minutes=int(offset_min))), f"UTC{sign}{hh:02d}:{mm:02d}"
    return timezone.utc, "UTC"


def safe_filename(title: str, fallback: str = "export") -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", title or "").strip("._-")
    return (name or fallback)[:64]


class _TimeFormatter:
    """Formats ms timestamps; caches the per-second prefix (rows from one
    packet share a timestamp, and datetime formatting dominates export cost)."""

    def __init__(self, tz: tzinfo):
        self._tz = tz
        self._sec: int | None = None
        self._prefix = ""

    def __call__(self, ts_ms: int) -> str:
        sec, ms = divmod(ts_ms, 1000)
        if sec != self._sec:
            self._sec = sec
            self._prefix = datetime.fromtimestamp(sec, self._tz).strftime("%Y-%m-%d %H:%M:%S")
        return f"{self._prefix}.{ms:03d}"


def _header(cells: Sequence[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(
        # Guard spreadsheet formula injection from sensor/node names
        ["'" + c if c[:1] in ("=", "+", "-", "@") else c for c in cells]
    )
    return buf.getvalue()


def _label(meta: dict) -> str:
    node = meta.get("node") or ""
    name = meta.get("name") or meta.get("id", "")
    units = meta.get("units") or ""
    label = f"{node} {name}".strip()
    return f"{label} ({units})" if units else label


def csv_wide(
    store: TimeSeriesStore,
    series: Sequence[dict],
    start_s: float,
    end_s: float,
    tz: tzinfo,
    tz_label: str,
) -> Iterator[bytes]:
    keys = [s["id"] for s in series]
    fmt = _TimeFormatter(tz)
    ncols = len(keys)
    parts = [_header([f"time ({tz_label})", "unix_s"] + [_label(s) for s in series])]
    size = len(parts[0])

    cur_ts: int | None = None
    row: list[str] = []
    for ts, idx, v in store.iter_merged(keys, start_s, end_s):
        if ts != cur_ts:
            if cur_ts is not None:
                line = f"{fmt(cur_ts)},{cur_ts / 1000:.3f},{','.join(row)}\n"
                parts.append(line)
                size += len(line)
                if size >= _CHUNK_BYTES:
                    yield "".join(parts).encode("utf-8")
                    parts, size = [], 0
            cur_ts = ts
            row = [""] * ncols
        row[idx] = repr(v)
    if cur_ts is not None:
        parts.append(f"{fmt(cur_ts)},{cur_ts / 1000:.3f},{','.join(row)}\n")
    if parts:
        yield "".join(parts).encode("utf-8")


def csv_long(
    store: TimeSeriesStore,
    series: Sequence[dict],
    start_s: float,
    end_s: float,
    tz: tzinfo,
    tz_label: str,
) -> Iterator[bytes]:
    keys = [s["id"] for s in series]
    fmt = _TimeFormatter(tz)
    # Pre-render the constant per-series columns once
    prefixes = []
    for s in series:
        buf = io.StringIO()
        csv.writer(buf, lineterminator="").writerow(
            [s["id"], s.get("node", ""), s.get("name", ""), s.get("units", "")]
        )
        prefixes.append(buf.getvalue())

    parts = [_header([f"time ({tz_label})", "unix_s", "series", "node", "sensor", "units", "value"])]
    size = len(parts[0])
    for ts, idx, v in store.iter_sequential(keys, start_s, end_s):
        line = f"{fmt(ts)},{ts / 1000:.3f},{prefixes[idx]},{v!r}\n"
        parts.append(line)
        size += len(line)
        if size >= _CHUNK_BYTES:
            yield "".join(parts).encode("utf-8")
            parts, size = [], 0
    if parts:
        yield "".join(parts).encode("utf-8")
