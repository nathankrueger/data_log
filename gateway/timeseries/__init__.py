"""
On-gateway history logger (opt-in).

Records selected sensor series to a local SQLite store with pre-aggregated
rollups so charts over weeks of data stay instant. Only the service is
imported eagerly; sqlite3 and the recorder thread load when recording starts.
"""

from gateway.timeseries.service import LoggerService

__all__ = ["LoggerService"]
