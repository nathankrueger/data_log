"""
Command handlers and registration for the outdoor sensor node.

Mirrors AB01's commands.cpp structure:
- Parameter table (alphabetically sorted)
- Command handlers
- Single commands_init() function to register everything

All JSON output uses alphabetically-sorted keys for CRC compatibility
with json.dumps(sort_keys=True).
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING

from utils.command_registry import CommandRegistry, CommandScope
from utils.params import ParamDef, param_get, param_set, params_list, cmds_list

if TYPE_CHECKING:
    from utils.node_state import NodeState

logger = logging.getLogger(__name__)

# Bandwidth encoding: matches AB01 convention (0/1/2 -> Hz)
BW_HZ_MAP = {0: 125000, 1: 250000, 2: 500000}
BW_CODE_MAP = {v: k for k, v in BW_HZ_MAP.items()}


# =============================================================================
# Command Handlers
# =============================================================================


def _handle_ping(_cmd: str, args: list[str]) -> None:
    """Handle ping command (broadcast or private)."""
    logger.info(f"[HANDLER] Ping: {args}")


def _handle_echo(_cmd: str, args: list[str]) -> dict:
    """Handle echo command - returns the argument."""
    data = args[0] if args else ""
    logger.info(f"[HANDLER] Echo: {data}")
    return {"r": data}


def _handle_getparam(params: list[ParamDef], _cmd: str, args: list[str]) -> dict:
    """Handle getparam command - returns single param value."""
    if not args:
        return {"e": "missing param name"}
    return param_get(params, args[0])


def _handle_setparam(params: list[ParamDef], _cmd: str, args: list[str]) -> None:
    """
    Handle setparam command.

    With early_ack=true, handler runs after ACK is sent.
    No return value - errors are logged locally.
    """
    if len(args) < 2:
        logger.warning("setparam: usage: name value")
        return
    param_set(params, args[0], args[1])


def _handle_getparams(params: list[ParamDef], _cmd: str, args: list[str]) -> dict:
    """Handle getparams command - returns paginated param list."""
    offset = int(args[0]) if args else 0
    return params_list(params, offset)


def _handle_getcmds(cmd_names: list[str], _cmd: str, args: list[str]) -> dict:
    """Handle getcmds command - returns paginated command list."""
    offset = int(args[0]) if args else 0
    return cmds_list(cmd_names, offset)


# =============================================================================
# Init Function
# =============================================================================


def commands_init(registry: CommandRegistry, state: NodeState) -> None:
    """
    Build param table and register all commands.

    Call once during setup. Matches AB01's commandsInit() pattern.

    Args:
        registry: CommandRegistry to register handlers with
        state: NodeState containing radio and node_id
    """
    radio = state.radio
    node_id = state.node_id
    # ─── Parameter Table ─────────────────────────────────────────────────────
    # MUST be in alphabetical order by name for CRC consistency
    params = [
        ParamDef(
            "bw",
            getter=lambda: BW_CODE_MAP.get(radio.signal_bandwidth, 0),
            setter=lambda v: setattr(radio, "signal_bandwidth", BW_HZ_MAP[int(v)]),
            min_val=0,
            max_val=2,
        ),
        ParamDef(
            "nodeid",
            getter=lambda: node_id,
            value_type=str,
        ),
        ParamDef(
            "sf",
            getter=lambda: radio.spreading_factor,
            setter=lambda v: setattr(radio, "spreading_factor", int(v)),
            min_val=7,
            max_val=12,
        ),
        ParamDef(
            "txpwr",
            getter=lambda: radio.tx_power,
            setter=lambda v: setattr(radio, "tx_power", int(v)),
            min_val=5,
            max_val=23,
        ),
    ]

    # ─── Sorted Command Name List ────────────────────────────────────────────
    # Built from command table, sorted for getcmds response
    cmd_names = sorted([
        "discover",
        "echo",
        "getcmds",
        "getparam",
        "getparams",
        "ping",
        "setparam",
    ])

    # ─── Command Table ───────────────────────────────────────────────────────
    # Format: (name, handler, scope, early_ack, ack_jitter)
    #
    # early_ack=True:  ACK sent before handler runs (fire-and-forget)
    # early_ack=False: Handler runs first, ACK sent after with response payload
    #
    # ack_jitter=True: Random delay before ACK (for broadcast discovery)
    commands = [
        # ping - broadcast and private variants
        ("ping", _handle_ping, CommandScope.BROADCAST, True, False),
        ("ping", _handle_ping, CommandScope.PRIVATE, True, False),
        # discover - broadcast with jitter to avoid ACK collisions
        ("discover", _handle_ping, CommandScope.BROADCAST, True, True),
        # echo - returns response payload
        ("echo", _handle_echo, CommandScope.PRIVATE, False, False),
        # param commands
        ("getcmds", partial(_handle_getcmds, cmd_names), CommandScope.ANY, False, False),
        ("getparam", partial(_handle_getparam, params), CommandScope.ANY, False, False),
        ("getparams", partial(_handle_getparams, params), CommandScope.ANY, False, False),
        # setparam - early_ack=True so radio param changes don't break ACK
        ("setparam", partial(_handle_setparam, params), CommandScope.PRIVATE, True, False),
    ]

    # Register all commands
    for name, handler, scope, early_ack, ack_jitter in commands:
        registry.register(name, handler, scope, early_ack=early_ack, ack_jitter=ack_jitter)

    logger.info(f"Registered {len(commands)} command handlers")
