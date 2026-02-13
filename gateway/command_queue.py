"""
Command queue with ACK-based reliability for gateway to node communication.

Classes:
    PendingCommand: Data container for a command awaiting ACK
    DiscoveryRequest: Coordination object for node discovery
    CommandQueue: Serial command queue with retry logic
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from utils.protocol import build_command_packet

logger = logging.getLogger(__name__)
cmd_logger = logging.getLogger("cmd_debug")


@dataclass
class PendingCommand:
    """A command awaiting ACK from the target node."""

    command_id: str
    cmd: str
    args: list[str]
    node_id: str
    packet: bytes
    next_retry_time: float
    retry_count: int = 0
    max_retries: int = 10
    first_sent_time: float = 0.0

    # Multi-ACK tracking (used when expected_acks > 1)
    expected_acks: int = 1
    acked_nodes: set[str] = field(default_factory=set)
    node_payloads: dict[str, dict] = field(default_factory=dict)


@dataclass
class DiscoveryRequest:
    """Request for node discovery, coordinated between HTTP and transceiver threads."""

    retries: int
    initial_retry_ms: int
    max_retry_ms: int
    retry_multiplier: float
    done: threading.Event
    nodes: list[str] = field(default_factory=list)
    error: str | None = None


class CommandQueue:
    """
    Serial command queue with ACK-based retirement.

    Commands are sent one at a time. After sending, the gateway waits for
    an ACK from the target node. If no ACK is received, the command is
    retried with multiplicative backoff until max_retries is reached.
    """

    def __init__(
        self,
        max_size: int = 128,
        max_retries: int = 10,
        initial_retry_ms: int = 500,
        max_retry_ms: int = 5000,
        retry_multiplier: float = 1.5,
        discovery_retries: int = 30,
        wait_timeout: float = 30.0,
    ):
        """
        Initialize the command queue.

        Args:
            max_size: Maximum number of pending commands
            max_retries: Maximum retry attempts before giving up
            initial_retry_ms: Initial retry delay in milliseconds
            max_retry_ms: Maximum retry delay (backoff cap)
            retry_multiplier: Backoff multiplier per retry (default 1.5)
            discovery_retries: Retry count for discovery operations
            wait_timeout: HTTP wait timeout for command responses (seconds)
        """
        self._queue: deque[PendingCommand] = deque()
        self._max_size = max_size
        self._current: PendingCommand | None = None
        self._lock = threading.Lock()
        self._max_retries = max_retries
        self._initial_retry_ms = initial_retry_ms
        self._max_retry_ms = max_retry_ms
        self._retry_multiplier = retry_multiplier
        self._discovery_retries = discovery_retries
        self._wait_timeout = wait_timeout
        self._completed_responses: dict[str, tuple[float, dict]] = {}
        self._response_ttl = 60.0  # seconds to keep completed responses

    # ─── Runtime Parameter Properties ───────────────────────────────────────

    @property
    def max_size(self) -> int:
        return self._max_size

    @max_size.setter
    def max_size(self, val: int) -> None:
        self._max_size = val

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @max_retries.setter
    def max_retries(self, val: int) -> None:
        self._max_retries = val
        self.validate_timeouts()

    @property
    def initial_retry_ms(self) -> int:
        return self._initial_retry_ms

    @initial_retry_ms.setter
    def initial_retry_ms(self, val: int) -> None:
        self._initial_retry_ms = val
        self.validate_timeouts()

    @property
    def max_retry_ms(self) -> int:
        return self._max_retry_ms

    @max_retry_ms.setter
    def max_retry_ms(self, val: int) -> None:
        self._max_retry_ms = val
        self.validate_timeouts()

    @property
    def retry_multiplier(self) -> float:
        return self._retry_multiplier

    @retry_multiplier.setter
    def retry_multiplier(self, val: float) -> None:
        self._retry_multiplier = val
        self.validate_timeouts()

    @property
    def discovery_retries(self) -> int:
        return self._discovery_retries

    @discovery_retries.setter
    def discovery_retries(self, val: int) -> None:
        self._discovery_retries = val

    @property
    def wait_timeout(self) -> float:
        return self._wait_timeout

    @wait_timeout.setter
    def wait_timeout(self, val: float) -> None:
        self._wait_timeout = val
        self.validate_timeouts()

    # ─── Timeout Validation ─────────────────────────────────────────────────────

    def calculate_max_retry_time(self) -> float:
        """
        Calculate max time (seconds) to exhaust all retries.

        This is the sum of all inter-retry delays, not including transmission time.
        """
        total_ms = 0.0
        for i in range(1, self._max_retries):  # delays between attempts
            delay = min(
                self._initial_retry_ms * (self._retry_multiplier ** (i - 1)),
                self._max_retry_ms,
            )
            total_ms += delay
        return total_ms / 1000

    def validate_timeouts(self) -> None:
        """Log warning if wait_timeout < max_retry_time."""
        max_retry_time = self.calculate_max_retry_time()
        if self._wait_timeout < max_retry_time:
            logger.warning(
                f"wait_timeout ({self._wait_timeout:.1f}s) < max_retry_time ({max_retry_time:.1f}s). "
                f"Commands may be cancelled before all retries are exhausted."
            )

    def add(
        self,
        cmd: str,
        args: list[str],
        node_id: str,
        max_retries: int | None = None,
        expected_acks: int = 1,
    ) -> str | None:
        """
        Add a command to the queue.

        Args:
            cmd: Command name
            args: Command arguments
            node_id: Target node ID (empty for broadcast)
            max_retries: Override default retry count (for fire-and-forget commands)
            expected_acks: Number of unique node ACKs required to complete (for broadcasts)

        Returns:
            Command ID for tracking, or None if queue is full
        """
        packet, command_id = build_command_packet(cmd, args, node_id)
        retries = max_retries if max_retries is not None else self._max_retries

        pending = PendingCommand(
            command_id=command_id,
            cmd=cmd,
            args=args,
            node_id=node_id,
            packet=packet,
            next_retry_time=0,  # Send immediately
            max_retries=retries,
            expected_acks=expected_acks,
        )

        with self._lock:
            if len(self._queue) >= self._max_size:
                return None
            self._queue.append(pending)
        cmd_logger.debug(
            "CMD_QUEUED cmd=%s target=%s id=%s",
            cmd, node_id or "broadcast", command_id,
        )
        return command_id

    def get_next_to_send(self) -> PendingCommand | None:
        """
        Get the next command to transmit, if ready.

        Returns:
            PendingCommand if one is ready to send, None otherwise
        """
        with self._lock:
            # If no current command, pop from queue
            if self._current is None and self._queue:
                self._current = self._queue.popleft()
                self._current.next_retry_time = 0  # Send immediately

            # Check if current command is ready to send (retry timer elapsed)
            if self._current and time.time() >= self._current.next_retry_time:
                return self._current
        return None

    def mark_sent(self) -> None:
        """Mark the current command as sent and schedule retry."""
        with self._lock:
            if self._current:
                self._current.retry_count += 1
                if self._current.retry_count == 1:
                    self._current.first_sent_time = time.time()
                # Exponential backoff with configurable multiplier, capped
                delay_ms = min(
                    self._initial_retry_ms
                    * (self._retry_multiplier ** (self._current.retry_count - 1)),
                    self._max_retry_ms,
                )
                self._current.next_retry_time = time.time() + (delay_ms / 1000)
                cmd_logger.debug(
                    "CMD_RETRY cmd=%s attempt=%d next_in=%dms",
                    self._current.cmd, self._current.retry_count, int(delay_ms),
                )

    def ack_received(
        self,
        command_id: str,
        node_id: str = "",
        payload: dict | None = None,
    ) -> PendingCommand | None:
        """
        Handle an ACK - retire the command if enough ACKs received.

        Args:
            command_id: ID from the ACK packet
            node_id: ID of the node that sent the ACK
            payload: Optional response payload from node

        Returns:
            The retired PendingCommand if matched and complete, None otherwise
        """
        with self._lock:
            if self._current and self._current.command_id == command_id:
                expected = self._current.expected_acks

                # Track which node ACK'd and its payload
                if node_id:
                    if node_id in self._current.acked_nodes:
                        # Already seen this node - duplicate ACK
                        logger.debug(f"Duplicate ACK from '{node_id}' ignored")
                        return None
                    self._current.acked_nodes.add(node_id)
                    if payload:
                        self._current.node_payloads[node_id] = payload

                # Check if we have enough ACKs
                ack_count = len(self._current.acked_nodes)

                # Backwards compatibility: if expected_acks=1 and no node tracking,
                # retire on first ACK (even if node_id not provided)
                should_retire = (
                    ack_count >= expected or
                    (expected == 1 and not node_id)  # Legacy call without node_id
                )

                if should_retire:
                    # Complete - retire the command
                    retired = self._current
                    if expected > 1:
                        logger.info(
                            f"Command '{retired.cmd}' ACK'd after "
                            f"{retired.retry_count} attempt(s) "
                            f"({ack_count}/{expected} ACKs)"
                        )
                    else:
                        logger.info(
                            f"Command '{retired.cmd}' ACK'd after "
                            f"{retired.retry_count} attempt(s)"
                        )
                    # Store response for retrieval
                    if expected > 1:
                        # Multi-ACK: store all node responses
                        self._completed_responses[command_id] = (
                            time.time(),
                            {
                                "acked_nodes": list(retired.acked_nodes),
                                "responses": retired.node_payloads,
                            },
                        )
                    else:
                        # Single ACK: store payload directly (backwards compatible)
                        self._completed_responses[command_id] = (
                            time.time(),
                            payload if payload is not None else {},
                        )
                    self._current = None
                    return retired
                else:
                    # Not enough ACKs yet - log progress
                    logger.info(
                        f"ACK from '{node_id}' for '{self._current.cmd}' "
                        f"({ack_count}/{expected})"
                    )
                    return None
        return None

    def check_expired(self) -> PendingCommand | None:
        """
        Check if the current command has exceeded max retries.

        Returns:
            The expired PendingCommand if one expired, None otherwise
        """
        with self._lock:
            if self._current and self._current.retry_count >= self._current.max_retries:
                expired = self._current
                self._current = None
                return expired
        return None

    def pending_count(self) -> int:
        """Return number of commands in queue (not including current)."""
        with self._lock:
            return len(self._queue)

    def has_current(self) -> bool:
        """Return True if there's a command currently being sent/retried."""
        with self._lock:
            return self._current is not None

    def cancel(self, command_id: str) -> bool:
        """
        Cancel a pending command, removing it from current or queue.

        Used by wait-mode handlers to prevent a timed-out command from
        blocking subsequent commands in the serial queue.

        Args:
            command_id: ID of the command to cancel

        Returns:
            True if the command was found and cancelled
        """
        with self._lock:
            if self._current and self._current.command_id == command_id:
                logger.info(f"Cancelled current command {command_id}")
                self._current = None
                return True
            original_len = len(self._queue)
            self._queue = deque(
                p for p in self._queue if p.command_id != command_id
            )
            if len(self._queue) < original_len:
                logger.info(f"Cancelled queued command {command_id}")
                return True
        return False

    def get_partial_acks(self, command_id: str) -> dict | None:
        """
        Get partial ACK info for a command that's still in progress or timed out.

        Used by HTTP handlers to return partial results when a multi-ACK
        broadcast times out before receiving all expected ACKs.

        Args:
            command_id: ID of the command

        Returns:
            Dict with acked_nodes, responses, expected_acks, or None if not found
        """
        with self._lock:
            if self._current and self._current.command_id == command_id:
                return {
                    "acked_nodes": list(self._current.acked_nodes),
                    "responses": dict(self._current.node_payloads),
                    "expected_acks": self._current.expected_acks,
                }
        return None

    def wait_for_response(self, command_id: str, timeout: float = 10.0) -> dict | None:
        """
        Wait for a command to complete and return its response payload.

        Args:
            command_id: ID of the command to wait for
            timeout: Maximum seconds to wait

        Returns:
            Response payload dict, or None if timeout/no payload
        """
        logger.info(f"Waiting for response to {command_id} (timeout={timeout}s)")
        deadline = time.time() + timeout
        poll_count = 0
        while time.time() < deadline:
            with self._lock:
                # Check if response is available
                if command_id in self._completed_responses:
                    _, payload = self._completed_responses.pop(command_id)
                    logger.info(f"Got response for {command_id}: {payload}")
                    return payload
                # Check if command completed without payload
                is_current = self._current and self._current.command_id == command_id
                in_queue = any(p.command_id == command_id for p in self._queue)
                if not is_current and not in_queue:
                    # Command completed but no response stored
                    logger.info(
                        f"Command {command_id} completed without response "
                        f"after {poll_count} polls ({time.time() - (deadline - timeout):.1f}s)"
                    )
                    return None
            poll_count += 1
            time.sleep(0.1)
        logger.warning(f"Timeout waiting for {command_id} after {poll_count} polls")
        return None

    def cleanup_old_responses(self) -> None:
        """Remove expired response payloads."""
        now = time.time()
        with self._lock:
            expired = [
                cid for cid, (ts, _) in self._completed_responses.items()
                if now - ts > self._response_ttl
            ]
            for cid in expired:
                del self._completed_responses[cid]
