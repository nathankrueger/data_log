"""Tests for CommandReceiver error recovery and packet processing."""

import json
import threading
import time

import pytest
from unittest.mock import MagicMock, patch, call

from node.data_log import CommandReceiver
from utils.command_registry import CommandRegistry
from utils.protocol import build_command_packet, calculate_crc32


@pytest.fixture
def mock_radio():
    """Create a mock radio that behaves like RFM9xRadio."""
    radio = MagicMock()
    radio.set_frequency = MagicMock()
    radio.listen = MagicMock()
    radio.rx_done = MagicMock(return_value=False)
    radio.receive = MagicMock(return_value=None)
    radio.send = MagicMock(return_value=True)
    radio.recover_rx = MagicMock()
    radio.hard_reset = MagicMock()
    return radio


@pytest.fixture
def registry():
    """Create a command registry with a simple ping handler."""
    reg = CommandRegistry("test_node")
    reg.register("ping", lambda cmd, args: None)
    return reg


@pytest.fixture
def receiver(mock_radio, registry):
    """Create a CommandReceiver for testing (not started as a thread)."""
    return CommandReceiver(
        radio=mock_radio,
        radio_lock=threading.Lock(),
        node_id="test_node",
        registry=registry,
        receive_timeout=0.5,
        broadcast_ack_jitter_sec=0,
    )


class TestProcessPacket:
    """Tests for _process_packet return values."""

    def test_valid_command_returns_true(self, receiver):
        """A valid command packet should return True."""
        packet_bytes, _ = build_command_packet("ping", [], node_id="test_node")
        assert receiver._process_packet(packet_bytes) is True

    def test_valid_broadcast_returns_true(self, receiver):
        """A broadcast command should return True."""
        packet_bytes, _ = build_command_packet("ping", [], node_id="")
        assert receiver._process_packet(packet_bytes) is True

    def test_command_for_other_node_returns_true(self, receiver):
        """A command for a different node is still valid (not garbage)."""
        packet_bytes, _ = build_command_packet("ping", [], node_id="other_node")
        assert receiver._process_packet(packet_bytes) is True

    def test_bare_int_returns_false(self, receiver):
        """Bare JSON integer (stuck FIFO) should return False."""
        packet = b"42"
        assert receiver._process_packet(packet) is False

    def test_bare_float_returns_false(self, receiver):
        """Bare JSON float (stuck FIFO) should return False."""
        packet = b"3.14"
        assert receiver._process_packet(packet) is False

    def test_bare_string_returns_false(self, receiver):
        """Bare JSON string should return False."""
        packet = b'"hello"'
        assert receiver._process_packet(packet) is False

    def test_json_array_returns_false(self, receiver):
        """JSON array should return False."""
        packet = b'[1,2,3]'
        assert receiver._process_packet(packet) is False

    def test_corrupt_bytes_returns_false(self, receiver):
        """Non-JSON binary data should return False."""
        packet = b'\xff\xfe\x00\x01'
        assert receiver._process_packet(packet) is False

    def test_valid_json_wrong_type_returns_false(self, receiver):
        """JSON dict without command fields should return False."""
        packet = json.dumps({"foo": "bar"}).encode()
        assert receiver._process_packet(packet) is False

    def test_bad_crc_returns_false(self, receiver):
        """Command packet with bad CRC should return False."""
        message = {
            "t": "cmd", "n": "test_node", "cmd": "ping",
            "a": [], "ts": int(time.time()), "c": "deadbeef",
        }
        packet = json.dumps(message, separators=(",", ":")).encode()
        assert receiver._process_packet(packet) is False

    def test_duplicate_returns_true(self, receiver, mock_radio):
        """Duplicate command (retransmission) should return True."""
        packet_bytes, _ = build_command_packet("ping", [], node_id="test_node")
        # First time
        assert receiver._process_packet(packet_bytes) is True
        # Second time (duplicate)
        assert receiver._process_packet(packet_bytes) is True


class TestRunLoop:
    """Tests for run() loop error tracking and radio reset.

    All tests exercise the real run() logic by mocking _receive_interruptible
    to feed a scripted sequence of packets, then checking recover_rx calls.
    """

    def _run_iterations(self, receiver, packets):
        """Feed packets into the real run() loop, then stop.

        Each entry in packets is either:
        - bytes: returned as a received packet
        - None: returned as a timeout
        - Exception instance: raised from _receive_interruptible
        """
        call_count = 0

        def fake_receive(timeout):
            nonlocal call_count
            if call_count >= len(packets):
                receiver._running = False
                return None
            item = packets[call_count]
            call_count += 1
            if isinstance(item, Exception):
                raise item
            return item

        receiver._receive_interruptible = fake_receive
        receiver.run()

    def test_consecutive_junk_triggers_reset(self, receiver, mock_radio):
        """recover_rx fires after N consecutive junk packets."""
        receiver._max_errors_before_reset = 3
        self._run_iterations(receiver, [b"42"] * 3)
        mock_radio.recover_rx.assert_called_once()

    def test_below_threshold_no_reset(self, receiver, mock_radio):
        """recover_rx does NOT fire when errors < threshold."""
        receiver._max_errors_before_reset = 10
        self._run_iterations(receiver, [b"42"] * 9)
        mock_radio.recover_rx.assert_not_called()

    def test_timeout_resets_counter(self, receiver, mock_radio):
        """Timeout (None) resets error counter, preventing reset."""
        receiver._max_errors_before_reset = 3
        # 2 junk → timeout resets → 2 more junk — never hits 3 consecutive
        self._run_iterations(receiver, [b"42", b"99", None, b"42", b"99"])
        mock_radio.recover_rx.assert_not_called()

    def test_valid_command_resets_counter(self, receiver, mock_radio):
        """Valid command resets error counter, preventing reset."""
        receiver._max_errors_before_reset = 3
        valid, _ = build_command_packet("ping", [], node_id="test_node")
        self._run_iterations(receiver, [b"42", b"99", valid, b"42", b"99"])
        mock_radio.recover_rx.assert_not_called()

    def test_interleaved_junk_and_valid_never_resets(self, receiver, mock_radio):
        """Valid command every 2 junk packets prevents reset from ever firing."""
        receiver._max_errors_before_reset = 3
        valid, _ = build_command_packet("ping", [], node_id="test_node")
        # Pattern repeated 10x: junk, junk, valid — never 3 consecutive junk
        self._run_iterations(receiver, [b"42", b"99", valid] * 10)
        mock_radio.recover_rx.assert_not_called()

    def test_counter_resets_after_radio_reset(self, receiver, mock_radio):
        """After reset fires, counter resets — needs N more junk to fire again."""
        receiver._max_errors_before_reset = 3
        # 3 junk → reset, then only 2 more — not enough for second reset
        self._run_iterations(receiver, [b"42"] * 3 + [b"99"] * 2)
        mock_radio.recover_rx.assert_called_once()

    def test_multiple_soft_resets(self, receiver, mock_radio):
        """Multiple batches of junk trigger multiple soft resets."""
        receiver._max_errors_before_reset = 3
        # 2 batches of 3: both are soft recoveries (count 1, 2)
        self._run_iterations(receiver, [b"42"] * 6)
        assert mock_radio.recover_rx.call_count == 2
        mock_radio.hard_reset.assert_not_called()

    def test_exception_increments_counter(self, receiver, mock_radio):
        """Exceptions from _receive_interruptible also count toward reset."""
        receiver._max_errors_before_reset = 3
        errors = [RuntimeError("SPI error")] * 3
        self._run_iterations(receiver, errors)
        mock_radio.recover_rx.assert_called_once()

    def test_mixed_junk_types(self, receiver, mock_radio):
        """Various junk types (int, float, array, binary) all count."""
        receiver._max_errors_before_reset = 4
        self._run_iterations(receiver, [b"42", b"3.14", b"[1,2]", b"\xff\xfe"])
        mock_radio.recover_rx.assert_called_once()

    def test_hard_reset_after_failed_soft_recoveries(self, receiver, mock_radio):
        """Hard reset fires after max_soft_recoveries failed soft recoveries."""
        receiver._max_errors_before_reset = 3
        receiver._max_soft_recoveries = 3
        # 9 junk: batch 1 → soft(1), batch 2 → soft(2), batch 3 → hard
        self._run_iterations(receiver, [b"42"] * 9)
        assert mock_radio.recover_rx.call_count == 2
        mock_radio.hard_reset.assert_called_once()

    def test_soft_recovery_count_resets_after_hard(self, receiver, mock_radio):
        """After hard reset, soft recovery count resets — next escalation is soft again."""
        receiver._max_errors_before_reset = 3
        receiver._max_soft_recoveries = 3
        # 9 → hard reset (resets soft count), then 3 more → soft again
        self._run_iterations(receiver, [b"42"] * 12)
        assert mock_radio.recover_rx.call_count == 3  # 2 before hard + 1 after
        mock_radio.hard_reset.assert_called_once()

    def test_valid_packet_resets_soft_recovery_count(self, receiver, mock_radio):
        """Valid packet resets soft recovery counter, preventing escalation."""
        receiver._max_errors_before_reset = 3
        receiver._max_soft_recoveries = 2
        valid, _ = build_command_packet("ping", [], node_id="test_node")
        # 3 junk → soft(1), valid resets both, 3 junk → soft(1) again, not hard
        self._run_iterations(receiver, [b"42"] * 3 + [valid] + [b"42"] * 3)
        assert mock_radio.recover_rx.call_count == 2
        mock_radio.hard_reset.assert_not_called()

    def test_timeout_resets_soft_recovery_count(self, receiver, mock_radio):
        """Timeout resets soft recovery counter, preventing escalation."""
        receiver._max_errors_before_reset = 3
        receiver._max_soft_recoveries = 2
        # 3 junk → soft(1), timeout resets, 3 junk → soft(1) again, not hard
        self._run_iterations(receiver, [b"42"] * 3 + [None] + [b"42"] * 3)
        assert mock_radio.recover_rx.call_count == 2
        mock_radio.hard_reset.assert_not_called()
