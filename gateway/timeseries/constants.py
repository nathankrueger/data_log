"""Shared constants for the history logger (kept import-light: no sqlite3)."""

# Pre-aggregation bucket widths in seconds (1 min, 5 min, 30 min, 3 h).
# Tier 0 means raw samples.
TIERS = (60, 300, 1800, 10800)

# Coarser tiers for multi-year windows (history kept forever). Not stored:
# a query sums the 3-hour buckets they contain (both divide evenly, and all
# buckets are aligned to the epoch), so the recorder and the rollup
# invariants are unchanged. 1 day / 1 week.
DERIVED_TIERS = (86400, 604800)
QUERY_TIERS = TIERS + DERIVED_TIERS
assert all(t % TIERS[-1] == 0 for t in DERIVED_TIERS), "derived tiers must nest the coarsest stored tier"

RETENTION_MIN_S = 10 * 60
RETENTION_MAX_S = 366 * 86400
RETENTION_DEFAULT_S = 7 * 86400
# max_age_s / retention_s of 0: keep everything (never pruned)
RETENTION_FOREVER = 0

# The recorder stops writing (and keeps buffering the newest samples) while
# the data directory's filesystem has less than this free, so history kept
# forever can never fill the gateway's SD card.
MIN_FREE_BYTES = 256 * 1024 * 1024

MAX_SERIES_PER_CHART = 8
MAX_CHARTS = 32

# Chart point budget bounds (the UI sends its plot width in CSS pixels)
POINTS_MIN = 50
POINTS_MAX = 4000

# Auto range: a chart's window starts at its oldest stored sample (never
# earlier than requested), but spans at least this long so new charts aren't
# stretched over a few seconds of data.
AUTO_MIN_SPAN_S = 300.0
