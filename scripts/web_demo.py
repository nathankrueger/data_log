#!/usr/bin/env python3
"""
Preview the gateway web UI with synthetic sensor data - no radio, no sensors.

    python scripts/web_demo.py                 # http://localhost:5002/
    python scripts/web_demo.py --port 5010 --backfill-hours 72

Simulates a handful of nodes: a local gateway BME280, two battery loggers
over LoRa (one drops out of range periodically), and a slow-reporting node.
Uses its own data directory (default /tmp/gateway-web-demo) so it never
touches the real logger database. Command endpoints respond, but nothing is
transmitted (there is no transceiver).

--backfill-hours writes that much synthetic history straight into the demo
database at startup so the charts' zoom levels and rollups have something
to show immediately.
"""

from __future__ import annotations

import argparse
import logging
import math
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.command_queue import CommandQueue  # noqa: E402
from gateway.sensor_collection import SensorDataCollector  # noqa: E402
from gateway.timeseries import LoggerService  # noqa: E402
from gateway.web import WebServer  # noqa: E402
from utils.gateway_state import GatewayState  # noqa: E402
from utils.protocol import SensorReading, make_sensor_id  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("web_demo")

DAY = 86400.0


def _wave(t: float, period: float, phase: float = 0.0) -> float:
    return math.sin(2 * math.pi * (t / period) + phase)


# node id -> (is_local, interval_s, sensor class, readings fn, base rssi)
def _gateway_readings(t: float):
    return [
        ("Temperature", "°C", 21.5 + 1.2 * _wave(t, DAY, -1.8) + random.gauss(0, 0.05)),
        ("Humidity", "%", 44 + 6 * _wave(t, DAY, 1.0) + random.gauss(0, 0.3)),
        ("Pressure", "hPa", 1013 + 4 * _wave(t, 3.3 * DAY) + random.gauss(0, 0.08)),
    ]


def _patio_readings(t: float):
    return [
        ("Temperature", "°C", 14 + 8 * _wave(t, DAY, -1.6) + random.gauss(0, 0.15)),
        ("Humidity", "%", 62 - 18 * _wave(t, DAY, -1.6) + random.gauss(0, 0.6)),
        ("Battery", "V", 4.15 - 0.55 * ((t / (5 * DAY)) % 1.0) + random.gauss(0, 0.004)),
    ]


def _trail_readings(t: float):
    spike = 6.0 if random.random() < 0.01 else 0.0
    return [
        ("Temperature", "°C", 9 + 6 * _wave(t, DAY, -1.3) + spike + random.gauss(0, 0.2)),
        ("Battery", "V", 3.95 - 0.4 * ((t / (3 * DAY)) % 1.0) + random.gauss(0, 0.004)),
        ("Accel Z", "g", 1.0 + random.gauss(0, 0.01)),
    ]


def _shed_readings(t: float):
    return [
        ("Temperature", "°C", 17 + 3 * _wave(t, DAY, -2.0) + random.gauss(0, 0.1)),
        ("Soil moisture", "%", 38 - 10 * ((t / (2 * DAY)) % 1.0) + random.gauss(0, 0.3)),
    ]


NODES = {
    "demo-gateway": (True, 5.0, "BME280", _gateway_readings, None),
    "patio": (False, 15.0, "BME280", _patio_readings, -78),
    "trail-cam": (False, 30.0, "MMA8452", _trail_readings, -104),
    "shed": (False, 120.0, "ADS1115", _shed_readings, -91),
}


def _readings(sensor_class: str, fn, t: float) -> list[SensorReading]:
    return [
        SensorReading(name=name, units=units, value=round(v, 3), sensor_class=sensor_class, timestamp=t)
        for name, units, v in fn(t)
    ]


def _out_of_range(node: str, t: float) -> bool:
    # trail-cam wanders out of range for 10 minutes every hour
    return node == "trail-cam" and (t % 3600) < 600


def run_nodes(collector: SensorDataCollector, stop: threading.Event) -> None:
    next_due = {n: time.time() + random.uniform(0, 3) for n in NODES}
    while not stop.is_set():
        now = time.time()
        for node, (local, interval, cls, fn, rssi) in NODES.items():
            if now < next_due[node]:
                continue
            next_due[node] = now + interval * random.uniform(0.95, 1.05)
            if _out_of_range(node, now):
                continue
            if not local and random.random() < 0.04:
                continue  # lost packet
            sig = None if rssi is None else int(rssi + random.gauss(0, 3))
            collector.add_readings(node, _readings(cls, fn, now), is_local=local, rssi=sig)
        stop.wait(0.25)


def backfill(service: LoggerService, collector: SensorDataCollector, hours: float) -> None:
    """Push synthetic history through the real recorder path (ingest + rollups)."""
    rec = service._recorder  # noqa: SLF001 - demo only
    if rec is None or hours <= 0:
        return
    end = time.time()
    t0 = end - hours * 3600
    log.info(f"Backfilling {hours:g}h of synthetic history…")
    count = 0
    unflushed = 0
    for node, (local, interval, cls, fn, _rssi) in NODES.items():
        t = t0
        while t < end:
            if not _out_of_range(node, t):
                dps = [
                    {"id": make_sensor_id(node, r.sensor_class, r.name), "value": r.value}
                    for r in _readings(cls, fn, t)
                ]
                rec.ingest(dps, t)
                count += len(dps)
                unflushed += len(dps)
            t += interval
            if unflushed >= 50_000:
                rec.flush()
                unflushed = 0
    rec.flush()
    log.info(f"Backfilled {count:,} samples")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5002)
    ap.add_argument("--data-dir", default="/tmp/gateway-web-demo")
    ap.add_argument("--backfill-hours", type=float, default=0.0)
    ap.add_argument("--flush", type=float, default=10.0, help="recorder flush interval (s)")
    args = ap.parse_args()

    state = GatewayState()
    state.node_id = "demo-gateway"
    collector = SensorDataCollector("demo-gateway", None, feed_size=200)
    collector.start()

    service = LoggerService(args.data_dir, collector=collector, flush_interval=args.flush)
    service.start()

    queue = CommandQueue(wait_timeout=5.0)
    state.command_queue = queue
    web = WebServer(port=args.port, command_queue=queue, config_path="")
    web.set_sensor_collector(collector)
    web.set_logger_service(service)
    web.set_gateway_state(state)
    web.start()

    if args.backfill_hours:
        # Order matters: history must be ingested oldest-first before any live
        # sample, or every backfilled sample lands in an already-open bucket.
        if service.enabled:
            service.set_enabled(False)
        now = time.time()
        for node, (local, _interval, cls, fn, rssi) in NODES.items():
            # Seed the series catalog (not recorded: recording is off)
            collector.add_readings(node, _readings(cls, fn, now), is_local=local, rssi=rssi)
        if not service.state()["charts"]:
            catalog = {c["id"]: c for c in collector.get_series_catalog()}
            temps = [k for k, c in catalog.items() if c["units"] == "°C"]
            volts = [k for k, c in catalog.items() if c["units"] == "V"]
            if temps:
                service.create_chart({"title": "Temperatures", "series": temps, "max_age_s": 14 * 86400})
            if volts:
                service.create_chart({"title": "Battery", "series": volts, "max_age_s": 14 * 86400})
        service.purge(None)
        service.set_enabled(True)
        backfill(service, collector, args.backfill_hours)

    stop = threading.Event()
    threading.Thread(target=run_nodes, args=(collector, stop), daemon=True).start()

    log.info(f"Demo UI at http://localhost:{args.port}/  (data in {args.data_dir}; Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        service.stop()
        web.stop()


if __name__ == "__main__":
    main()
