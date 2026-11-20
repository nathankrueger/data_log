"""Tests for LoRa link-quality local sensors (LoRaRSSI / LoRaSNR)."""

import threading
import time
from unittest.mock import MagicMock

import pytest

from gateway import sensor_collection
from gateway.sensor_collection import (
    LocalSensorEntry,
    LocalSensorReader,
    SensorDataCollector,
    instantiate_sensors,
)
from gateway.transceiver import LoRaTransceiver
from sensors import EVERY_PACKET, LoRaRSSI, LoRaSNR, Sensor
from utils.gateway_state import GatewayState
from utils.protocol import SensorReading, build_ack_packet, build_lora_packets


class RecordingCollector:
    """Stand-in for SensorDataCollector that records add_readings calls."""

    def __init__(self):
        self.calls: list[tuple[str, list[SensorReading], bool]] = []
        self.posted = threading.Event()

    def add_readings(self, node_id, readings, is_local=False, **kwargs):
        self.calls.append((node_id, list(readings), is_local))
        self.posted.set()


class FakeHardwareSensor(Sensor):
    def init(self):
        pass

    def read(self):
        return (21.5,)

    def get_names(self):
        return ("Temperature",)

    def get_units(self):
        return ("C",)


# =============================================================================
# LoRaLinkSensor
# =============================================================================


class TestLinkSensor:
    @pytest.mark.parametrize("nodes", [None, []])
    def test_nodes_required(self, nodes):
        with pytest.raises(ValueError, match="nodes"):
            LoRaRSSI(nodes=nodes)

    def test_ignores_unlisted_nodes(self):
        s = LoRaRSSI(nodes=["ab01-7"])
        s.record_packet("ab01-4", -80, 9.0, 100.0)
        assert s.drain(latest_only=False) == []

    def test_rssi_and_snr_pick_their_own_value(self):
        rssi, snr = LoRaRSSI(nodes=["n"]), LoRaSNR(nodes=["n"])
        for s in (rssi, snr):
            s.record_packet("n", -91, 6.25, 100.0)
        (_, [r]), = rssi.drain(latest_only=False)
        (_, [n]), = snr.drain(latest_only=False)
        assert (r.value, r.units, r.name, r.sensor_class) == (-91, "dBm", "RSSI", "LoRaRSSI")
        assert (n.value, n.units, n.name, n.sensor_class) == (6.25, "dB", "SNR", "LoRaSNR")

    def test_missing_value_is_not_recorded(self):
        s = LoRaSNR(nodes=["n"])
        s.record_packet("n", -90, None, 100.0)
        assert s.drain(latest_only=False) == []

    def test_every_packet_drain_keeps_all_with_receive_times(self):
        s = LoRaRSSI(nodes=["n"])
        for i, rssi in enumerate([-90, -95, -99]):
            s.record_packet("n", rssi, 5.0, 100.0 + i)
        (node, readings), = s.drain(latest_only=False)
        assert node == "n"
        assert [(r.timestamp, r.value) for r in readings] == [
            (100.0, -90), (101.0, -95), (102.0, -99)
        ]

    def test_latest_only_drain_keeps_most_recent_per_node(self):
        s = LoRaRSSI(nodes=["a", "b"])
        s.record_packet("a", -90, 5.0, 100.0)
        s.record_packet("b", -70, 5.0, 101.0)
        s.record_packet("a", -97, 5.0, 102.0)
        result = dict(s.drain(latest_only=True))
        assert [(r.timestamp, r.value) for r in result["a"]] == [(102.0, -97)]
        assert [(r.timestamp, r.value) for r in result["b"]] == [(101.0, -70)]

    def test_drain_clears_so_nothing_repeats(self):
        s = LoRaRSSI(nodes=["n"])
        s.record_packet("n", -90, 5.0, 100.0)
        assert s.drain(latest_only=True)
        assert s.drain(latest_only=True) == []
        assert not s.has_pending()

    def test_record_sets_wake_event(self):
        s = LoRaRSSI(nodes=["n"])
        event = threading.Event()
        s.set_wake_event(event)
        s.record_packet("other", -90, 5.0, 100.0)
        assert not event.is_set()
        s.record_packet("n", -90, 5.0, 100.0)
        assert event.is_set()

    def test_not_pollable(self):
        s = LoRaRSSI(nodes=["n"])
        assert s.read() == () and s.get_names() == () and s.get_units() == ()


# =============================================================================
# instantiate_sensors
# =============================================================================


class TestInstantiate:
    @pytest.fixture(autouse=True)
    def fake_hw_class(self, monkeypatch):
        real = sensor_collection.get_sensor_class
        monkeypatch.setattr(
            sensor_collection,
            "get_sensor_class",
            lambda name: FakeHardwareSensor if name == "FakeHW" else real(name),
        )

    def test_per_sensor_interval_and_default(self):
        entries = instantiate_sensors(
            [
                {"class": "FakeHW"},
                {"class": "LoRaRSSI", "interval_sec": 60, "config": {"nodes": ["n"]}},
            ],
            default_interval=3600,
        )
        assert [(e.class_name, e.interval_sec) for e in entries] == [
            ("FakeHW", 3600), ("LoRaRSSI", 60)
        ]

    def test_every_packet_allowed_for_link_sensors(self):
        (entry,) = instantiate_sensors(
            [{"class": "LoRaSNR", "interval_sec": 0, "config": {"nodes": ["n"]}}]
        )
        assert entry.every_packet

    @pytest.mark.parametrize("bad", [0, -5, "fast", True])
    def test_invalid_hardware_interval_falls_back(self, bad):
        (entry,) = instantiate_sensors(
            [{"class": "FakeHW", "interval_sec": bad}], default_interval=30
        )
        assert entry.interval_sec == 30

    def test_link_sensor_without_nodes_is_skipped(self):
        assert instantiate_sensors([{"class": "LoRaRSSI", "interval_sec": 0}]) == []


# =============================================================================
# LocalSensorReader scheduling
# =============================================================================


def make_reader(entries, collector, gateway_state=None):
    return LocalSensorReader("pi5-gateway", entries, collector, gateway_state)


class TestReaderScheduling:
    def test_rate_limited_logs_latest_once_per_interval(self):
        """10 packets inside one interval -> one row (the latest)."""
        sensor = LoRaRSSI(nodes=["ab01-7"])
        entry = LocalSensorEntry(sensor, "LoRaRSSI", interval_sec=60)
        collector = RecordingCollector()
        reader = make_reader([entry], collector)

        for i in range(10):
            sensor.record_packet("ab01-7", -80 - i, 5.0, 1000.0 + i)
        reader._run_due(now=1010.0)

        assert len(collector.calls) == 1
        node, readings, is_local = collector.calls[0]
        assert node == "ab01-7" and is_local is False
        assert [(r.timestamp, r.value) for r in readings] == [(1009.0, -89)]

    def test_rate_faster_than_packets_logs_no_repeats(self):
        """Interval shorter than the packet rate -> only new packets are logged."""
        sensor = LoRaRSSI(nodes=["ab01-7"])
        entry = LocalSensorEntry(sensor, "LoRaRSSI", interval_sec=6)
        collector = RecordingCollector()
        reader = make_reader([entry], collector)

        sensor.record_packet("ab01-7", -90, 5.0, 1000.0)
        for now in range(1000, 1060, 6):  # ten due ticks, one packet
            reader._run_due(now=float(now))
        assert len(collector.calls) == 1

    def test_not_due_yet_holds_samples(self):
        sensor = LoRaRSSI(nodes=["n"])
        entry = LocalSensorEntry(sensor, "LoRaRSSI", interval_sec=60, next_due=2000.0)
        collector = RecordingCollector()
        reader = make_reader([entry], collector)

        sensor.record_packet("n", -90, 5.0, 1000.0)
        reader._run_due(now=1500.0)
        assert collector.calls == []
        reader._run_due(now=2000.0)
        assert len(collector.calls) == 1

    def test_every_packet_mode_logs_each_packet(self):
        sensor = LoRaSNR(nodes=["ab01-8"])
        entry = LocalSensorEntry(sensor, "LoRaSNR", interval_sec=EVERY_PACKET)
        collector = RecordingCollector()
        reader = make_reader([entry], collector)

        sensor.record_packet("ab01-8", -90, 7.0, 1000.0)
        sensor.record_packet("ab01-8", -91, 6.5, 1120.0)
        reader._run_due(now=1121.0)

        ((node, readings, _),) = collector.calls
        assert node == "ab01-8"
        assert [(r.timestamp, r.value) for r in readings] == [(1000.0, 7.0), (1120.0, 6.5)]

    def test_hardware_posted_under_gateway_link_under_node(self):
        hw = LocalSensorEntry(FakeHardwareSensor(), "FakeHW", interval_sec=60)
        link_sensor = LoRaRSSI(nodes=["ab01-7"])
        link = LocalSensorEntry(link_sensor, "LoRaRSSI", interval_sec=EVERY_PACKET)
        collector = RecordingCollector()
        state = GatewayState()
        reader = make_reader([hw, link], collector, state)

        link_sensor.record_packet("ab01-7", -90, 5.0, 1000.0)
        reader._run_due(now=1000.0)

        by_node = {node: (readings, is_local) for node, readings, is_local in collector.calls}
        assert by_node["pi5-gateway"][1] is True
        assert by_node["ab01-7"][1] is False
        # Only hardware readings reach the OLED's local-sensor page
        assert [s.name for s in state.get_local_sensors()] == ["Temperature"]

    def test_only_timed_entries_set_the_wait(self):
        link = LocalSensorEntry(LoRaRSSI(nodes=["n"]), "LoRaRSSI", interval_sec=0)
        reader = make_reader([link], RecordingCollector())
        assert reader._time_until_next_due() is None

    def test_running_reader_wakes_on_packet(self):
        """Every-packet mode publishes promptly, without waiting for a timer."""
        sensor = LoRaRSSI(nodes=["ab01-7"])
        hw = LocalSensorEntry(FakeHardwareSensor(), "FakeHW", interval_sec=3600)
        link = LocalSensorEntry(sensor, "LoRaRSSI", interval_sec=EVERY_PACKET)
        collector = RecordingCollector()
        reader = make_reader([hw, link], collector)
        reader.start()
        try:
            assert collector.posted.wait(2)  # startup hardware read
            collector.posted.clear()
            sensor.record_packet("ab01-7", -88, 5.0, time.time())
            assert collector.posted.wait(2)
            assert collector.calls[-1][0] == "ab01-7"
        finally:
            reader.stop()
            reader.join(timeout=2)
        assert not reader.is_alive()


# =============================================================================
# End to end: series naming and transceiver feed
# =============================================================================


class TestIntegration:
    def test_dashboard_series_is_per_node(self):
        collector = SensorDataCollector("pi5-gateway", dashboard_client=None)
        # The listener receives the same datapoints that are posted to the dashboard
        posted = []
        collector.set_listener(lambda dps, ts: posted.extend(dps))
        for node in ("ab01-7", "ab01-8"):
            collector.add_readings(
                node,
                [SensorReading("RSSI", "dBm", -90, "LoRaRSSI", 1000.0),
                 SensorReading("SNR", "dB", 7.5, "LoRaSNR", 1000.0)],
                is_local=False,
            )
        ids = {dp["id"]: dp["name"] for dp in posted}
        assert ids == {
            "ab01_7_lorarssi_rssi": "ab01-7 RSSI",
            "ab01_7_lorasnr_snr": "ab01-7 SNR",
            "ab01_8_lorarssi_rssi": "ab01-8 RSSI",
            "ab01_8_lorasnr_snr": "ab01-8 SNR",
        }

        # The web UI feed keeps the node in its own column and the bare reading name
        feed = {dp["id"]: (dp["node"], dp["name"]) for dp in collector.get_recent_readings()}
        assert feed == {
            "ab01_7_lorarssi_rssi": ("ab01-7", "RSSI"),
            "ab01_7_lorasnr_snr": ("ab01-7", "SNR"),
            "ab01_8_lorarssi_rssi": ("ab01-8", "RSSI"),
            "ab01_8_lorasnr_snr": ("ab01-8", "SNR"),
        }

    def test_link_readings_are_not_packets(self):
        """A rate-limited publish describes an old packet: it must not make a
        silent node look just heard, count as a packet, or be recorded "now"."""
        collector = SensorDataCollector("pi5-gateway", dashboard_client=None)
        collector.add_readings(
            "ab01-7",
            [SensorReading("Temperature", "C", 20.0, "BME280TempPressureHumidity", 0)],
            rssi=-90,
        )
        (before,) = collector.get_nodes()
        recorded = []
        collector.set_listener(lambda dps, ts: recorded.extend((dp["id"], ts) for dp in dps))

        heard = before["last_seen"] - 600
        collector.add_readings(
            "ab01-7", [SensorReading("RSSI", "dBm", -95, "LoRaRSSI", heard)], measured=True
        )

        (after,) = collector.get_nodes()
        assert after["last_seen"] == before["last_seen"]
        assert after["packets"] == before["packets"] == 1
        assert after["rssi"] == -90
        assert recorded == [("ab01_7_lorarssi_rssi", heard)]
        series = {s["id"]: s for s in collector.get_series_catalog()}
        assert series["ab01_7_lorarssi_rssi"]["ts"] == heard

    def test_link_reading_for_unknown_node_creates_it(self):
        """Every-packet drains can beat the packet's own add_readings call."""
        collector = SensorDataCollector("pi5-gateway", dashboard_client=None)
        heard = time.time()
        collector.add_readings(
            "ab01-9", [SensorReading("SNR", "dB", 6.0, "LoRaSNR", heard)], measured=True
        )
        (node,) = collector.get_nodes()
        assert node["id"] == "ab01-9" and node["last_seen"] == heard and node["packets"] == 0

    @pytest.fixture
    def transceiver_parts(self):
        radio = MagicMock()
        radio.get_last_rssi.return_value = -93
        radio.get_last_snr.return_value = 4.75
        rssi = LoRaRSSI(nodes=["ab01-7"])
        snr = LoRaSNR(nodes=["ab01-7"])
        command_queue = MagicMock()
        command_queue.ack_received.return_value = None
        transceiver = LoRaTransceiver(
            radio, MagicMock(), command_queue, link_sensors=[rssi, snr]
        )
        return transceiver, rssi, snr

    def test_transceiver_feeds_sensor_packets(self, transceiver_parts):
        transceiver, rssi, snr = transceiver_parts
        reading = SensorReading("Temperature", "C", 20.0, "BME280TempPressureHumidity", 0)
        for packet in build_lora_packets("ab01-7", [reading]):
            transceiver._process_received_packet(packet)

        (_, [r]), = rssi.drain(latest_only=False)
        (_, [s]), = snr.drain(latest_only=False)
        assert (r.value, s.value) == (-93, 4.75)

    def test_transceiver_ignores_acks_and_junk(self, transceiver_parts):
        transceiver, rssi, _ = transceiver_parts
        ack = build_ack_packet("1700000000_abcd", "ab01-7")
        transceiver._process_received_packet(ack)
        transceiver._process_received_packet(b"\xff" * 7)
        assert not rssi.has_pending()


class TestGatewayStateMerge:
    def test_update_merges_by_name(self):
        state = GatewayState()
        state.update_local_sensors([("Temperature", 20.0, "C"), ("Humidity", 40.0, "%")])
        state.update_local_sensors([("Humidity", 45.0, "%")])
        assert [(s.name, s.value) for s in state.get_local_sensors()] == [
            ("Temperature", 20.0), ("Humidity", 45.0)
        ]
