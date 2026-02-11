"""
params.py - Generic parameter registry with JSON response builders

Provides a table-driven parameter system for get/set/list operations.
Mirrors AB01's shared/params.h for behavioral equivalence.

All JSON output uses alphabetically-sorted keys for CRC compatibility
with json.dumps(sort_keys=True).
"""

import json
import logging
from dataclasses import dataclass
from typing import Callable

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


def param_get(params: list[ParamDef], name: str) -> dict:
    """
    Get a single parameter value.

    Returns {"name": value} or {"e": "error message"}.
    """
    for p in params:
        if p.name == name:
            return {name: p.getter()}
    return {"e": "unknown param"}


def param_set(params: list[ParamDef], name: str, value_str: str) -> None:
    """
    Set a parameter value.

    Validates, applies, and calls on_set callback.
    With early_ack=true, no return value is used - errors are logged locally.
    """
    # Find param
    p = None
    for param in params:
        if param.name == name:
            p = param
            break

    if p is None:
        logger.warning(f"setparam: unknown param '{name}'")
        return

    if p.setter is None:
        logger.warning(f"setparam: read-only param '{name}'")
        return

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
        return

    # Range check
    if p.min_val is not None and val < p.min_val:
        logger.warning(f"setparam: {name}={val} below min {p.min_val}")
        return
    if p.max_val is not None and val > p.max_val:
        logger.warning(f"setparam: {name}={val} above max {p.max_val}")
        return

    # Apply value
    p.setter(value_str)
    logger.info(f"setparam: {name}={p.getter()}")

    # Call on_set callback if present
    if p.on_set:
        p.on_set(name)


def params_list(params: list[ParamDef], offset: int = 0) -> dict:
    """
    List parameters with values, paginated.

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
        test[p.name] = p.getter()
        encoded = json.dumps({"m": 0, "p": test}, separators=(",", ":"))

        if len(encoded) > MAX_RESPONSE_PAYLOAD and result:
            more = 1
            break

        result[p.name] = p.getter()

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
