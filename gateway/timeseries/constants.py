"""Shared constants for the history logger (kept import-light: no sqlite3)."""

# Pre-aggregation bucket widths in seconds (1 min, 5 min, 30 min, 3 h).
# Tier 0 means raw samples.
TIERS = (60, 300, 1800, 10800)

RETENTION_MIN_S = 10 * 60
RETENTION_MAX_S = 366 * 86400
RETENTION_DEFAULT_S = 7 * 86400

MAX_SERIES_PER_CHART = 8
MAX_CHARTS = 32

# Chart point budget bounds (the UI sends its plot width in CSS pixels)
POINTS_MIN = 50
POINTS_MAX = 4000

# Auto range: a chart's window starts at its oldest stored sample (never
# earlier than requested), but spans at least this long so new charts aren't
# stretched over a few seconds of data.
AUTO_MIN_SPAN_S = 300.0
