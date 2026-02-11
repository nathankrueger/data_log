"""
Gateway parameter definitions and registry.

Provides get/set endpoints for gateway configuration with:
- Radio params (sf, bw, txpwr, freqs) - STAGED, require rcfg_radio to apply
- Command_server params - IMMEDIATE, apply to runtime on set

NO params auto-persist. Use savecfg endpoint to persist ALL params.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from utils.radio_state import BW_CODE_MAP, BW_HZ_MAP

if TYPE_CHECKING:
    from utils.gateway_state import GatewayState

logger = logging.getLogger(__name__)


# Config key mappings for radio params (used by rcfg_radio persistence)
RADIO_PARAM_CONFIG_KEYS = {
    "sf": "lora.spreading_factor",
    "bw": "lora.signal_bandwidth",
    "txpwr": "lora.tx_power",
    "n2gfreq": "lora.n2g_frequency_mhz",
    "g2nfreq": "lora.g2n_frequency_mhz",
}


@dataclass
class GatewayParamDef:
    """Definition of a gateway parameter."""

    name: str
    getter: Callable[[], int | float | str]
    setter: Callable[[Any], None] | None = None  # None = read-only
    config_key: str | None = None  # Key path for persistence
    min_val: int | float | None = None
    max_val: int | float | None = None
    value_type: type = int  # int, float, str
    staged: bool = False  # If True, stores in pending (no immediate apply/persist)


def build_gateway_params(state: GatewayState) -> list[GatewayParamDef]:
    """
    Build parameter definitions from gateway state.

    Radio params use staged config (require rcfg_radio to apply).
    Command_server params apply immediately.
    """
    params: list[GatewayParamDef] = []

    # Radio params - STAGED (use effective getters, stage in pending)
    rs = state.radio_state
    if rs:
        params.extend([
            GatewayParamDef(
                name="sf",
                getter=lambda rs=rs: rs.get_effective_sf(),
                setter=lambda v, rs=rs: rs.set_pending("sf", str(int(v))),
                config_key="lora.spreading_factor",
                min_val=7,
                max_val=12,
                staged=True,
            ),
            GatewayParamDef(
                name="bw",
                getter=lambda rs=rs: rs.get_effective_bw(),
                setter=lambda v, rs=rs: rs.set_pending("bw", str(int(v))),
                config_key="lora.signal_bandwidth",
                min_val=0,
                max_val=2,
                staged=True,
            ),
            GatewayParamDef(
                name="txpwr",
                getter=lambda rs=rs: rs.get_effective_txpwr(),
                setter=lambda v, rs=rs: rs.set_pending("txpwr", str(int(v))),
                config_key="lora.tx_power",
                min_val=5,
                max_val=23,
                staged=True,
            ),
            GatewayParamDef(
                name="n2g_freq",
                getter=lambda rs=rs: rs.get_effective_n2g_freq_hz() / 1e6,
                setter=lambda v, rs=rs: rs.set_pending("n2gfreq", str(int(float(v) * 1e6))),
                config_key="lora.n2g_frequency_mhz",
                value_type=float,
                staged=True,
            ),
            GatewayParamDef(
                name="g2n_freq",
                getter=lambda rs=rs: rs.get_effective_g2n_freq_hz() / 1e6,
                setter=lambda v, rs=rs: rs.set_pending("g2nfreq", str(int(float(v) * 1e6))),
                config_key="lora.g2n_frequency_mhz",
                value_type=float,
                staged=True,
            ),
        ])

    # Read-only params
    params.append(
        GatewayParamDef(
            name="nodeid",
            getter=lambda state=state: state.node_id,
            setter=None,
            value_type=str,
        )
    )

    # Command server params - IMMEDIATE (not staged)
    cq = state.command_queue
    if cq:
        params.extend([
            GatewayParamDef(
                name="max_queue_size",
                getter=lambda cq=cq: cq.max_size,
                setter=lambda v, cq=cq: setattr(cq, "max_size", int(v)),
                config_key="command_server.max_queue_size",
                min_val=1,
                max_val=1000,
                staged=False,
            ),
            GatewayParamDef(
                name="max_retries",
                getter=lambda cq=cq: cq.max_retries,
                setter=lambda v, cq=cq: setattr(cq, "max_retries", int(v)),
                config_key="command_server.max_retries",
                min_val=1,
                max_val=100,
                staged=False,
            ),
            GatewayParamDef(
                name="initial_retry_ms",
                getter=lambda cq=cq: cq.initial_retry_ms,
                setter=lambda v, cq=cq: setattr(cq, "initial_retry_ms", int(v)),
                config_key="command_server.initial_retry_ms",
                min_val=100,
                max_val=30000,
                staged=False,
            ),
            GatewayParamDef(
                name="retry_multiplier",
                getter=lambda cq=cq: cq.retry_multiplier,
                setter=lambda v, cq=cq: setattr(cq, "retry_multiplier", float(v)),
                config_key="command_server.retry_multiplier",
                min_val=1.0,
                max_val=5.0,
                value_type=float,
                staged=False,
            ),
            GatewayParamDef(
                name="max_retry_ms",
                getter=lambda cq=cq: cq.max_retry_ms,
                setter=lambda v, cq=cq: setattr(cq, "max_retry_ms", int(v)),
                config_key="command_server.max_retry_ms",
                min_val=1000,
                max_val=60000,
                staged=False,
            ),
            GatewayParamDef(
                name="discovery_retries",
                getter=lambda cq=cq: cq.discovery_retries,
                setter=lambda v, cq=cq: setattr(cq, "discovery_retries", int(v)),
                config_key="command_server.discovery_retries",
                min_val=1,
                max_val=100,
                staged=False,
            ),
        ])

    return params


class GatewayParamRegistry:
    """Registry for gateway parameters with get/set and persistence support."""

    def __init__(self, params: list[GatewayParamDef], config_path: str):
        self._params = {p.name: p for p in params}
        self._config_path = config_path

    def get_all(self) -> dict[str, Any]:
        """Get all parameter values."""
        return {name: p.getter() for name, p in sorted(self._params.items())}

    def get(self, name: str) -> tuple[Any | None, str | None]:
        """Get a parameter value. Returns (value, None) or (None, error_msg)."""
        p = self._params.get(name)
        if p is None:
            return None, f"unknown param: {name}"
        return p.getter(), None

    def set(self, name: str, value_str: str) -> tuple[Any | None, str | None]:
        """
        Set a parameter value.

        For staged params: stores in pending (no hardware apply).
        For immediate params: applies to runtime immediately.

        NO params auto-persist. Use savecfg endpoint to persist.

        Returns (new_value, None) on success or (None, error_msg) on failure.
        """
        p = self._params.get(name)
        if p is None:
            return None, f"unknown param: {name}"
        if p.setter is None:
            return None, f"read-only: {name}"

        # Parse value
        try:
            if p.value_type is int:
                val = int(value_str)
            elif p.value_type is float:
                val = float(value_str)
            else:
                val = value_str
        except ValueError:
            return None, f"invalid value: {value_str}"

        # Range check
        if p.min_val is not None and val < p.min_val:
            return None, f"range: {p.min_val}..{p.max_val}"
        if p.max_val is not None and val > p.max_val:
            return None, f"range: {p.min_val}..{p.max_val}"

        # Apply via setter (NO persist - savecfg does that)
        p.setter(val)

        if p.staged:
            # Staged params: setter stores in pending, return pending value
            logger.info(f"gateway param staged: {name}={val}")
            return val, None
        else:
            # Immediate params: setter applies to runtime, NO persist
            logger.info(f"gateway param set: {name}={val}")
            return p.getter(), None

    def is_staged(self, name: str) -> bool:
        """Check if a parameter uses staged config."""
        p = self._params.get(name)
        return p.staged if p else False

    def get_config_key(self, name: str) -> str | None:
        """Get the config key for a parameter."""
        p = self._params.get(name)
        return p.config_key if p else None
