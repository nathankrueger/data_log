"""
Radio state encapsulation for sensor nodes.

Consolidates radio hardware reference, frequencies, and staged configuration
into a single class with thread-safe accessors.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from radio import RFM9xRadio

logger = logging.getLogger(__name__)

# Bandwidth encoding: matches AB01 convention (0/1/2 -> Hz)
BW_HZ_MAP = {0: 125000, 1: 250000, 2: 500000}
BW_CODE_MAP = {v: k for k, v in BW_HZ_MAP.items()}


class RadioState:
    """
    Encapsulates all radio-related state for a sensor node.

    Thread-safe container for:
    - Radio hardware reference (RFM9xRadio)
    - Operating frequencies (n2g_freq, g2n_freq in MHz)
    - Staged/pending radio configuration changes

    The staged config pattern:
    1. setparam stores values via set_pending() (doesn't touch hardware)
    2. rcfg_radio command calls apply_pending() (applies to hardware)
    3. This prevents ACK failures when changing radio settings
    """

    def __init__(
        self,
        radio: RFM9xRadio,
        n2g_freq: float,
        g2n_freq: float,
    ):
        """
        Initialize RadioState.

        Args:
            radio: Radio hardware instance
            n2g_freq: Node-to-Gateway frequency in MHz (sensor broadcasts, ACKs)
            g2n_freq: Gateway-to-Node frequency in MHz (command reception)
        """
        self._radio = radio
        self._n2g_freq = n2g_freq
        self._g2n_freq = g2n_freq
        self._pending: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def radio(self) -> RFM9xRadio:
        """Get the radio hardware instance."""
        return self._radio

    # ─── Frequency Properties (thread-safe) ─────────────────────────────────

    @property
    def n2g_freq(self) -> float:
        """Get Node-to-Gateway frequency in MHz (thread-safe)."""
        with self._lock:
            return self._n2g_freq

    @n2g_freq.setter
    def n2g_freq(self, value: float) -> None:
        """Set Node-to-Gateway frequency in MHz (thread-safe)."""
        with self._lock:
            self._n2g_freq = value

    @property
    def g2n_freq(self) -> float:
        """Get Gateway-to-Node frequency in MHz (thread-safe)."""
        with self._lock:
            return self._g2n_freq

    @g2n_freq.setter
    def g2n_freq(self, value: float) -> None:
        """Set Gateway-to-Node frequency in MHz (thread-safe)."""
        with self._lock:
            self._g2n_freq = value

    # ─── Active Radio Parameters (from hardware) ────────────────────────────

    @property
    def spreading_factor(self) -> int:
        """Get current spreading factor from hardware."""
        return self._radio.spreading_factor

    @property
    def signal_bandwidth(self) -> int:
        """Get current signal bandwidth in Hz from hardware."""
        return self._radio.signal_bandwidth

    @property
    def bandwidth_code(self) -> int:
        """Get current bandwidth as code (0=125kHz, 1=250kHz, 2=500kHz)."""
        return BW_CODE_MAP.get(self._radio.signal_bandwidth, 0)

    @property
    def tx_power(self) -> int:
        """Get current TX power from hardware."""
        return self._radio.tx_power

    # ─── Staged/Pending Configuration ───────────────────────────────────────

    def set_pending(self, name: str, value: str) -> None:
        """
        Store a pending value for a staged radio param.

        Thread-safe. Value is stored but not applied to hardware until
        apply_pending() is called.
        """
        with self._lock:
            self._pending[name] = value

    def get_pending(self, name: str) -> str | None:
        """Get pending value if set, None otherwise (thread-safe)."""
        with self._lock:
            return self._pending.get(name)

    def clear_pending(self, name: str) -> None:
        """Clear a pending value after it's been applied (thread-safe)."""
        with self._lock:
            self._pending.pop(name, None)

    def get_all_pending(self) -> dict[str, str]:
        """Get a copy of all pending values (thread-safe)."""
        with self._lock:
            return self._pending.copy()

    def clear_all_pending(self) -> None:
        """Clear all pending values (thread-safe)."""
        with self._lock:
            self._pending.clear()

    def has_pending(self) -> bool:
        """Check if there are any pending values (thread-safe)."""
        with self._lock:
            return len(self._pending) > 0

    def apply_pending(self) -> list[str]:
        """
        Apply all pending radio config changes to hardware.

        Returns list of applied changes as "name=value" strings.
        Raises exception on hardware error (pending values are NOT cleared
        on error to allow retry).
        """
        pending = self.get_all_pending()
        if not pending:
            return []

        applied = []
        for name, value in pending.items():
            if name == "sf":
                self._radio.spreading_factor = int(value)
            elif name == "bw":
                self._radio.signal_bandwidth = BW_HZ_MAP[int(value)]
            elif name == "txpwr":
                self._radio.tx_power = int(value)
            elif name == "n2gfreq":
                self.n2g_freq = int(value) / 1e6  # Hz to MHz
            elif name == "g2nfreq":
                self.g2n_freq = int(value) / 1e6  # Hz to MHz
            self.clear_pending(name)
            applied.append(f"{name}={value}")
            logger.info(f"RadioState: applied {name}={value}")

        return applied

    # ─── Effective Value Accessors (pending if set, else active) ────────────

    def get_effective_sf(self) -> int:
        """Get SF - pending value if staged, otherwise active hardware value."""
        pending = self.get_pending("sf")
        return int(pending) if pending else self.spreading_factor

    def get_effective_bw(self) -> int:
        """Get BW code - pending value if staged, otherwise active hardware value."""
        pending = self.get_pending("bw")
        return int(pending) if pending else self.bandwidth_code

    def get_effective_txpwr(self) -> int:
        """Get TX power - pending value if staged, otherwise active hardware value."""
        pending = self.get_pending("txpwr")
        return int(pending) if pending else self.tx_power

    def get_effective_n2g_freq_hz(self) -> int:
        """Get N2G freq in Hz - pending value if staged, otherwise active."""
        pending = self.get_pending("n2gfreq")
        return int(pending) if pending else int(self.n2g_freq * 1e6)

    def get_effective_g2n_freq_hz(self) -> int:
        """Get G2N freq in Hz - pending value if staged, otherwise active."""
        pending = self.get_pending("g2nfreq")
        return int(pending) if pending else int(self.g2n_freq * 1e6)
