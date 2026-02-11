"""
params.py - Generic parameter registry with JSON response builders

Provides a table-driven parameter system for get/set/list operations.
Mirrors AB01's shared/params.h for behavioral equivalence.

All JSON output uses alphabetically-sorted keys for CRC compatibility
with json.dumps(sort_keys=True).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from utils.radio_state import RadioState

logger = logging.getLogger(__name__)

# Max payload size for paginated responses (conservative ACK budget)
MAX_RESPONSE_PAYLOAD = 170


@dataclass
class ParamDef:
    """Definition of a runtime-tunable parameter."""

    name: str
    getter: Callable[[], int | float | str]
    setter: Callable[[str], None] | None = None  # None = read-only
    min_val: int | float | None = None
    max_val: int | float | None = None
    value_type: type = int
    on_set: Callable[[str], None] | None = None  # Optional callback after set
    config_key: str | None = None  # Dot-notation path for persistence (e.g., "lora.tx_power")
    staged: bool = False  # If True, setparam stores in pending, applied by rcfg_radio


def param_get(
    params: list[ParamDef], name: str, radio_state: RadioState | None = None
) -> dict:
    """
    Get a single parameter value.

    For staged params with radio_state, returns pending value if set.
    Returns {"name": value} or {"e": "error message"}.

    Args:
        params: List of ParamDef to search
        name: Parameter name to get
        radio_state: Optional RadioState for staged param pending values
    """
    for p in params:
        if p.name == name:
            # For staged params, return pending value if set
            if p.staged and radio_state:
                pending = radio_state.get_pending(name)
                if pending is not None:
                    # Convert to typed value for response
                    if p.value_type is int:
                        return {name: int(pending)}
                    elif p.value_type is float:
                        return {name: float(pending)}
                    else:
                        return {name: pending}
            return {name: p.getter()}
    return {"e": "unknown param"}


def param_set(
    params: list[ParamDef],
    name: str,
    value_str: str,
    radio_state: RadioState | None = None,
) -> dict:
    """
    Set a parameter value.

    For staged params with radio_state, stores in pending without applying
    (use rcfg_radio to apply). For non-staged params, validates, applies,
    and calls on_set callback.

    Args:
        params: List of ParamDef to search
        name: Parameter name to set
        value_str: Value as string
        radio_state: Optional RadioState for staged param pending storage

    Returns {"name": value} on success, {"e": "error"} on failure.
    """
    # Find param
    p = None
    for param in params:
        if param.name == name:
            p = param
            break

    if p is None:
        logger.warning(f"setparam: unknown param '{name}'")
        return {"e": f"unknown param: {name}"}

    if p.setter is None:
        logger.warning(f"setparam: read-only param '{name}'")
        return {"e": f"read-only: {name}"}

    # Parse value
    try:
        if p.value_type is int:
            val = int(value_str)
        elif p.value_type is float:
            val = float(value_str)
        else:
            val = value_str
    except ValueError:
        logger.warning(f"setparam: invalid value '{value_str}' for '{name}'")
        return {"e": f"invalid value: {value_str}"}

    # Range check
    if p.min_val is not None and val < p.min_val:
        logger.warning(f"setparam: {name}={val} below min {p.min_val}")
        return {"e": f"{name} below min {p.min_val}"}
    if p.max_val is not None and val > p.max_val:
        logger.warning(f"setparam: {name}={val} above max {p.max_val}")
        return {"e": f"{name} above max {p.max_val}"}

    # For staged params with radio_state, store in pending instead of applying
    if p.staged and radio_state:
        radio_state.set_pending(name, value_str)
        logger.info(f"setparam: {name}={val} (staged)")
        return {name: val}

    # Apply value immediately for non-staged params
    p.setter(value_str)
    logger.info(f"setparam: {name}={p.getter()}")

    # Call on_set callback if present
    if p.on_set:
        p.on_set(name)

    return {name: p.getter()}


def _get_param_value(
    p: ParamDef, radio_state: RadioState | None = None
) -> int | float | str:
    """Get parameter value, using pending value for staged params if set."""
    if p.staged and radio_state:
        pending = radio_state.get_pending(p.name)
        if pending is not None:
            if p.value_type is int:
                return int(pending)
            elif p.value_type is float:
                return float(pending)
            else:
                return pending
    return p.getter()


def params_list(
    params: list[ParamDef],
    offset: int = 0,
    radio_state: RadioState | None = None,
) -> dict:
    """
    List parameters with values, paginated.

    For staged params with radio_state, returns pending value if set.
    Returns {"m": 0|1, "p": {"name": value, ...}}
    where "m" is 1 if more pages remain.

    Params must be pre-sorted alphabetically for CRC consistency.
    """
    if offset < 0:
        offset = 0

    result: dict = {}
    more = 0

    for p in params[offset:]:
        # Build test result to check size
        test = dict(result)
        test[p.name] = _get_param_value(p, radio_state)
        encoded = json.dumps({"m": 0, "p": test}, separators=(",", ":"))

        if len(encoded) > MAX_RESPONSE_PAYLOAD and result:
            more = 1
            break

        result[p.name] = _get_param_value(p, radio_state)

    return {"m": more, "p": result}


def cmds_list(cmd_names: list[str], offset: int = 0) -> dict:
    """
    List command names, paginated.

    Returns {"c": ["cmd1", "cmd2", ...], "m": 0|1}
    where "m" is 1 if more pages remain.

    cmd_names must be pre-sorted alphabetically for CRC consistency.
    """
    if offset < 0:
        offset = 0

    result: list[str] = []
    more = 0

    for cmd_name in cmd_names[offset:]:
        # Build test result to check size
        test = result + [cmd_name]
        encoded = json.dumps({"c": test, "m": 0}, separators=(",", ":"))

        if len(encoded) > MAX_RESPONSE_PAYLOAD and result:
            more = 1
            break

        result.append(cmd_name)

    return {"c": result, "m": more}


def params_save(params: list[ParamDef], config_path: str) -> bool:
    """
    Save all persistable params to config file.

    Only saves writable params that have a config_key defined.
    Uses atomic file writes to prevent corruption on power loss.

    Args:
        params: List of ParamDef to check for persistence
        config_path: Path to the config JSON file

    Returns:
        True if any changes were written, False if unchanged or no persistable params.
    """
    from utils.config_persistence import update_config_file

    saved_any = False
    for p in params:
        if p.config_key and p.setter:  # Only save writable params with config_key
            value = p.getter()
            update_config_file(config_path, p.config_key, value)
            logger.info(f"savecfg: {p.config_key}={value}")
            saved_any = True

    return saved_any
