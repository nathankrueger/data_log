"""
Reliability tests for the gateway runtime core: command queue timing and
memory bounds, transceiver radio-health recovery, packet type validation,
display robustness and the process lock location.

The transceiver tests drive the real LoRaTransceiver thread with a scripted
fake radio, so no hardware is needed.
"""

import itertools
import json
import logging
import threading
import time
import types

import pytest

import gateway.command_queue as command_queue_mod
import gateway.transceiver as transceiver_mod
from gateway.command_queue import CommandQueue
from gateway.display_pages import GatewayLocalSensors, LastPacketPage
from gateway.sensor_collection import SensorDataCollector
from gateway.transceiver import LoRaTransceiver
from utils.gateway_state import GatewayState
from utils.process_lock import lock_path_for
from utils.protocol import (
    SensorReading,
    build_ack_packet,
    build_lora_packets,
    calculate_crc32,
    parse_ack_packet,
    parse_lora_packet,
)


def wait_until(predicate, timeout=10.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# =============================================================================
# Command queue
# =============================================================================


class TestCommandQueueTiming:
    def test_wall_clock_steps_do_not_affect_retry_timing(self, monkeypatch):
        """A day-long NTP step must not fire retries early or stall them."""
        wall = {"now": 1_700_000_000.0}
        fake_time = types.SimpleNamespace(
            time=lambda: wall["now"], monotonic=time.monotonic, sleep=time.sleep
        )
        monkeypatch.setattr(command_queue_mod, "time", fake_time)
        q = CommandQueue(max_retries=3, initial_retry_ms=200, max_retry_ms=200, retry_multiplier=1.0)
        q.add("ping", [], "n1")
        assert q.get_next_to_send() is not None
        q.mark_sent()
        wall["now"] += 86400  # clock jumps forward a day
        assert q.get_next_to_send() is None  # still inside the 200 ms window
        wall["now"] -= 2 * 86400  # ...then back two days
        assert wait_until(lambda: q.get_next_to_send() is not None, timeout=2)

    def test_wait_for_response_ignores_wall_clock_jumps(self, monkeypatch):
        wall = {"now": 1_700_000_000.0}

        def jumpy():
            wall["now"] += 3600  # every read jumps an hour
            return wall["now"]

        monkeypatch.setattr(
            command_queue_mod, "time", types.SimpleNamespace(time=jumpy, monotonic=time.monotonic, sleep=time.sleep)
        )
        q = CommandQueue()
        cid = q.add("ping", [], "n1")
        t0 = time.monotonic()
        assert q.wait_for_response(cid, timeout=0.5) is None
        assert time.monotonic() - t0 >= 0.45  # waited its real timeout, not zero

    def test_last_attempt_gets_its_ack_window(self):
        q = CommandQueue(max_retries=1, initial_retry_ms=300, min_retry_ms=0, max_retry_ms=300)
        cid = q.add("echo", [], "n1")
        assert q.get_next_to_send() is not None
        q.mark_sent()  # the only attempt
        assert q.check_expired() is None  # not expired the instant it was sent
        assert q.get_next_to_send() is None  # and never re-sent past max_retries
        retired = q.ack_received(cid, node_id="n1", payload={"r": "ok"})
        assert retired is not None
        assert q.wait_for_response(cid, timeout=1) == {"r": "ok"}

    def test_command_expires_after_final_window(self):
        q = CommandQueue(max_retries=1, initial_retry_ms=100, min_retry_ms=0, max_retry_ms=100)
        q.add("echo", [], "n1")
        q.get_next_to_send()
        q.mark_sent()
        assert q.check_expired() is None
        assert wait_until(lambda: q.check_expired() is not None, timeout=2)
        assert not q.has_current()

    def test_max_retry_time_includes_final_window(self):
        q = CommandQueue(max_retries=1, initial_retry_ms=500, min_retry_ms=0, max_retry_ms=5000)
        assert q.calculate_max_retry_time() == pytest.approx(0.5)

    def test_uncollected_responses_are_bounded_and_swept(self):
        q = CommandQueue(max_size=10_000)
        for i in range(700):  # fire-and-forget commands nobody waits for
            cid = q.add("ping", [], f"n{i}")
            q.get_next_to_send()
            q.mark_sent()
            assert q.ack_received(cid, node_id=f"n{i}") is not None
        assert len(q._completed_responses) == CommandQueue._MAX_STORED_RESPONSES
        q._response_ttl = 0.0
        q.maybe_cleanup(interval=0)
        assert q._completed_responses == {}

    def test_concurrent_producers_waiters_and_ackers(self):
        q = CommandQueue(max_size=1000, max_retries=50, initial_retry_ms=5, max_retry_ms=5, wait_timeout=10)
        results = []
        errors = []

        def transceiver():
            # Plays the radio: sends whatever is current and ACKs it at once
            end = time.monotonic() + 15
            while time.monotonic() < end and len(results) < 40:
                cur = q.get_next_to_send()
                if cur is not None:
                    q.mark_sent()
                    q.ack_received(cur.command_id, node_id=cur.node_id or "x", payload={"for": cur.node_id})
                q.check_expired()
                q.maybe_cleanup(interval=0.05)
                time.sleep(0.001)

        def client(k):
            try:
                for i in range(10):
                    cid = q.add("echo", [], f"node{k}-{i}")
                    resp = q.wait_for_response(cid, timeout=10)
                    results.append(resp == {"for": f"node{k}-{i}"})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=transceiver, daemon=True)]
        threads += [threading.Thread(target=client, args=(k,), daemon=True) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not any(t.is_alive() for t in threads), "deadlock"
        assert not errors
        assert len(results) == 40 and all(results)


# =============================================================================
# Transceiver radio health
# =============================================================================


SLOW_NONE = "slow-none"  # a normal receive timeout
INSTANT_NONE = "instant-none"  # rx_done fired, packet dropped (CRC error)
RAISE = "raise"


class FakeRadio:
    """Scripted radio: receive() plays the script, then idles."""

    def __init__(self, script=(), send_ok=True, reconnect_results=()):
        self._script = iter(list(script))
        self._lock = threading.Lock()
        self.resets = 0
        self.sent = []
        self.send_ok = send_ok
        self.frequency = None
        # Results for successive reconnect() calls; True once exhausted.
        # "raise" makes that attempt raise instead of returning.
        self._reconnect_results = iter(reconnect_results)
        self.reconnect_calls = 0

    def receive(self, timeout=0.1):
        with self._lock:
            item = next(self._script, SLOW_NONE)
        if item == SLOW_NONE:
            time.sleep(0.01)
            return None
        if item == INSTANT_NONE:
            return None
        if item == RAISE:
            raise OSError("USB device not found")
        return item

    def hard_reset(self):
        self.resets += 1

    def reconnect(self):
        self.reconnect_calls += 1
        result = next(self._reconnect_results, True)
        if result == RAISE:
            raise OSError("libusb: no device")
        return result

    def get_last_rssi(self):
        return -80

    def get_last_snr(self):
        return 7.5

    def set_frequency(self, mhz):
        self.frequency = mhz

    def send(self, data):
        self.sent.append(data)
        return self.send_ok


def sensor_packet(node="n1", value=1.0):
    reading = SensorReading(name="T", units="C", value=value, sensor_class="BME280TempPressureHumidity", timestamp=time.time())
    return build_lora_packets(node, [reading])[0]


@pytest.fixture
def run_transceiver():
    started = []

    def start(radio, queue=None, collector=None, silence_reset_s=0.0):
        tx = LoRaTransceiver(
            radio,
            collector or SensorDataCollector("gw", None),
            command_queue=queue or CommandQueue(),
        )
        tx.silence_reset_s = silence_reset_s
        tx.start()
        started.append(tx)
        return tx

    yield start
    for tx in started:
        tx.stop()


class TestTransceiverHealth:
    def test_garbage_burst_triggers_one_reset(self, run_transceiver, caplog):
        radio = FakeRadio([b"\xff\xff\xff"] * 10)
        with caplog.at_level(logging.WARNING, logger="gateway.transceiver"):
            run_transceiver(radio)
            assert wait_until(lambda: radio.resets == 1)
            time.sleep(0.1)
        assert radio.resets == 1
        invalid_logs = [r for r in caplog.records if "Invalid LoRa packet" in r.getMessage()]
        assert len(invalid_logs) == 1  # rate-limited, not one per packet

    def test_instant_none_storm_triggers_reset(self, run_transceiver):
        # 30, not 10: on a loaded Pi an "instant" return can occasionally take
        # >5 ms and count as a normal timeout (which doesn't clear the count)
        radio = FakeRadio([INSTANT_NONE] * 30)
        run_transceiver(radio)
        assert wait_until(lambda: radio.resets >= 1)

    def test_valid_packets_keep_the_counter_down(self, run_transceiver):
        collector = SensorDataCollector("gw", None)
        script = [b"junk"] * 9 + [sensor_packet()] + [b"junk"] * 9 + [sensor_packet(value=2.0)]
        radio = FakeRadio(script)
        run_transceiver(radio, collector=collector)
        assert wait_until(lambda: collector.feed_info()["seq"] == 2)
        time.sleep(0.05)
        assert radio.resets == 0

    def test_silence_resets_back_off(self, run_transceiver):
        radio = FakeRadio()
        tx = run_transceiver(radio, silence_reset_s=0.05)
        assert wait_until(lambda: radio.resets >= 2, timeout=5)
        assert tx._silence_window > 0.05  # backing off while it stays quiet

    def test_failed_transmissions_trigger_reset(self, run_transceiver):
        q = CommandQueue(max_retries=10, initial_retry_ms=1, min_retry_ms=0, max_retry_ms=1, retry_multiplier=1.0)
        q.add("ping", [], "n1")
        radio = FakeRadio(send_ok=False)
        run_transceiver(radio, queue=q)
        assert wait_until(lambda: radio.resets >= 1)
        assert len(radio.sent) >= transceiver_mod.BAD_TX_RESET_COUNT

    @pytest.fixture
    def fast_recovery(self, monkeypatch):
        """Recovery mode without the real 3 s pause between attempts."""
        monkeypatch.setattr(
            transceiver_mod,
            "time",
            types.SimpleNamespace(
                time=time.time, monotonic=time.monotonic, sleep=lambda s: time.sleep(0.001)
            ),
        )

    def test_radio_error_reconnects_in_process(self, run_transceiver, fast_recovery):
        """An unplugged adapter is rebuilt in place; the thread never exits."""
        collector = SensorDataCollector("gw", None)
        radio = FakeRadio([RAISE, sensor_packet()], reconnect_results=[False, False, True])
        tx = run_transceiver(radio, collector=collector)
        assert wait_until(lambda: collector.feed_info()["seq"] == 1)
        assert radio.reconnect_calls == 3 and tx.reconnects == 1
        assert not tx._radio_disconnected and tx.is_alive()

    def test_reconnect_retunes_to_n2g(self, run_transceiver, fast_recovery):
        """If the radio died mid-command (cache on G2N), recovery listens on N2G."""
        radio = FakeRadio([RAISE])
        radio.frequency = 915.5
        tx = run_transceiver(radio)
        assert wait_until(lambda: tx.reconnects == 1)
        assert radio.frequency == tx._n2g_freq

    def test_heartbeat_beats_while_reconnecting(self, run_transceiver, fast_recovery):
        """A long unplug must not trip the main loop's stall watchdog."""
        radio = FakeRadio([RAISE], reconnect_results=itertools.repeat(False))
        tx = run_transceiver(radio)
        assert wait_until(lambda: tx._radio_disconnected and radio.reconnect_calls > 5)
        first = tx.heartbeat
        assert wait_until(lambda: tx.heartbeat > first)
        assert tx.is_alive()
        tx.stop()  # while the fast sleep is still patched in
        assert not tx.is_alive()

    def test_reconnect_that_raises_does_not_kill_the_thread(self, run_transceiver, fast_recovery):
        collector = SensorDataCollector("gw", None)
        radio = FakeRadio([RAISE, sensor_packet()], reconnect_results=[RAISE, RAISE, True])
        tx = run_transceiver(radio, collector=collector)
        assert wait_until(lambda: collector.feed_info()["seq"] == 1)
        assert tx.is_alive() and tx.reconnects == 1

    def test_health_counters_restart_after_reconnect(self, run_transceiver, fast_recovery):
        """Unreadable packets before the disconnect don't count against the new radio."""
        radio = FakeRadio([b"junk"] * 9 + [RAISE] + [b"junk"] * 9)
        tx = run_transceiver(radio)
        assert wait_until(lambda: tx.reconnects == 1)
        time.sleep(0.1)
        assert radio.resets == 0

    def test_heartbeat_advances(self, run_transceiver):
        tx = run_transceiver(FakeRadio())
        first = tx.heartbeat
        assert wait_until(lambda: tx.heartbeat > first)


# =============================================================================
# Packet validation (CRC-valid but wrong types)
# =============================================================================


def signed(message):
    message = dict(message)
    message["c"] = calculate_crc32(message)
    return json.dumps(message).encode()


class TestPacketTypes:
    @pytest.mark.parametrize("bad", [
        {"n": 5, "t": 0, "r": []},
        {"n": ["x"], "t": 0, "r": []},
        {"n": "", "t": 0, "r": []},
        {"n": "a", "t": "soon", "r": []},
        {"n": "a", "t": 0, "r": "nope"},
        {"n": "a", "t": 0, "r": [{"s": 1, "k": 7, "u": "C", "v": 1}]},
        {"n": "a", "t": 0, "r": [{"s": 1, "k": "T", "u": None, "v": 1}]},
    ])
    def test_sensor_packets_with_wrong_types_are_rejected(self, bad):
        assert parse_lora_packet(signed(bad)) is None

    @pytest.mark.parametrize("bad", [
        {"t": "ack", "id": 5, "n": "a"},
        {"t": "ack", "id": "1_abcd", "n": ["a"]},
        {"t": "ack", "id": "1_abcd", "n": "a", "p": [1, 2]},
    ])
    def test_ack_packets_with_wrong_types_are_rejected(self, bad):
        assert parse_ack_packet(signed(bad)) is None

    def test_valid_packets_still_parse(self):
        node, readings = parse_lora_packet(sensor_packet("patio", 21.5))
        assert node == "patio" and readings[0].value == 21.5
        ack = parse_ack_packet(build_ack_packet("1_abcd", "patio", {"r": 1}))
        assert ack.node_id == "patio" and ack.payload == {"r": 1}


# =============================================================================
# Display and process lock
# =============================================================================


class TestDisplayRobustness:
    @pytest.mark.parametrize("value", [None, "n/a", 3, 2.25, True])
    def test_pages_render_any_value(self, value):
        state = GatewayState()
        state.update_last_packet(node_id="n", rssi=-80, sensor_name="T", sensor_value=value, sensor_units="C")
        state.update_local_sensors([("T", value, "C")])
        assert LastPacketPage(state).get_lines()
        assert GatewayLocalSensors(state).get_lines()


def test_process_lock_lives_outside_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = lock_path_for("gateway")
    assert path == str(tmp_path / ".cache" / "data_log" / "gateway.lock")
    assert (tmp_path / ".cache" / "data_log").is_dir()
