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
from utils.params import ParamDef, param_get, param_set, params_list, cmds_list, params_save
from utils.radio_state import RadioState

if TYPE_CHECKING:
    from utils.node_state import NodeState

logger = logging.getLogger(__name__)


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


def _handle_getparam(
    params: list[ParamDef], radio_state: RadioState, _cmd: str, args: list[str]
) -> dict:
    """Handle getparam command - returns single param value."""
    if not args:
        return {"e": "missing param name"}
    return param_get(params, args[0], radio_state)


def _handle_setparam(
    params: list[ParamDef], radio_state: RadioState, _cmd: str, args: list[str]
) -> dict:
    """
    Handle setparam command.

    With early_ack=false (late ACK), returns response with value or error.
    For staged params (radio config), stores in pending - use rcfg_radio to apply.
    """
    if len(args) < 2:
        return {"e": "usage: name value"}
    return param_set(params, args[0], args[1], radio_state)


def _handle_getparams(
    params: list[ParamDef], radio_state: RadioState, _cmd: str, args: list[str]
) -> dict:
    """Handle getparams command - returns paginated param list."""
    offset = int(args[0]) if args else 0
    return params_list(params, offset, radio_state)


def _handle_getcmds(cmd_names: list[str], _cmd: str, args: list[str]) -> dict:
    """Handle getcmds command - returns paginated command list."""
    offset = int(args[0]) if args else 0
    return cmds_list(cmd_names, offset)


def _handle_savecfg(
    params: list[ParamDef], config_path: str, _cmd: str, args: list[str]
) -> dict:
    """
    Handle savecfg command - persist current params to config file.

    Returns {"r": "saved"} if changes written, {"r": "unchanged"} otherwise.
    """
    saved = params_save(params, config_path)
    result = "saved" if saved else "unchanged"
    logger.info(f"[HANDLER] savecfg: {result}")
    return {"r": result}


def _handle_rcfg_radio(radio_state: RadioState, _cmd: str, args: list[str]) -> dict:
    """
    Apply pending radio config changes.

    Staged radio params (bw, sf, txpwr, n2gfreq, g2nfreq) are stored in pending
    by setparam. This command applies them to the radio hardware via
    RadioState.apply_pending().

    Uses early_ack=true so ACK is sent before radio changes take effect.
    Returns {"r": "applied_params"} or {"r": "nothing"} if no pending changes.
    """
    try:
        applied = radio_state.apply_pending()
        if not applied:
            logger.info("[HANDLER] rcfg_radio: nothing to apply")
            return {"r": "nothing"}

        result = ", ".join(applied)
        logger.info(f"[HANDLER] rcfg_radio: {result}")
        return {"r": result}
    except Exception as e:
        logger.error(f"[HANDLER] rcfg_radio failed: {e}")
        return {"e": str(e)}


def _handle_rssi(radio_state: RadioState, _cmd: str, args: list[str]) -> dict:
    """
    Handle rssi command - returns RSSI of the received command packet.

    Uses early_ack=false so the RSSI measurement is for the command packet itself.
    Returns {"r": rssi_value} in dBm.
    """
    rssi = radio_state.radio.get_last_rssi()
    if rssi is None:
        rssi = 0
    logger.info(f"[HANDLER] RSSI: {rssi} dBm")
    return {"r": rssi}


# =============================================================================
# Init Function
# =============================================================================


def commands_init(registry: CommandRegistry, state: NodeState) -> None:
    """
    Build param table and register all commands.

    Call once during setup. Matches AB01's commandsInit() pattern.

    Args:
        registry: CommandRegistry to register handlers with
        state: NodeState containing radio_state, node_id, config_path
    """
    radio_state = state.radio_state
    node_id = state.node_id
    config_path = state.config_path

    # ─── Parameter Table ─────────────────────────────────────────────────────
    # MUST be in alphabetical order by name for CRC consistency
    #
    # STAGED RADIO CONFIG: Radio params (bw, sf, txpwr, n2gfreq, g2nfreq) have
    # staged=True. setparam stores in pending, rcfg_radio applies to hardware.
    # This prevents ACK failures when changing radio settings.
    #
    # Getters use get_effective_*() to return pending value if staged.
    # Frequencies use Hz integers for consistency with AB01.
    params = [
        ParamDef(
            "bw",
            getter=lambda: radio_state.get_effective_bw(),
            setter=lambda v: None,  # Applied by rcfg_radio
            min_val=0,
            max_val=2,
            config_key="lora.bandwidth",
            staged=True,
        ),
        ParamDef(
            "g2nfreq",
            getter=lambda: radio_state.get_effective_g2n_freq_hz(),
            setter=lambda v: None,  # Applied by rcfg_radio
            min_val=902000000,
            max_val=928000000,
            value_type=int,
            config_key="lora.g2n_frequency_hz",
            staged=True,
        ),
        ParamDef(
            "n2gfreq",
            getter=lambda: radio_state.get_effective_n2g_freq_hz(),
            setter=lambda v: None,  # Applied by rcfg_radio
            min_val=902000000,
            max_val=928000000,
            value_type=int,
            config_key="lora.n2g_frequency_hz",
            staged=True,
        ),
        ParamDef(
            "nodeid",
            getter=lambda: node_id,
            value_type=str,
        ),
        ParamDef(
            "sf",
            getter=lambda: radio_state.get_effective_sf(),
            setter=lambda v: None,  # Applied by rcfg_radio
            min_val=7,
            max_val=12,
            config_key="lora.spreading_factor",
            staged=True,
        ),
        ParamDef(
            "txpwr",
            getter=lambda: radio_state.get_effective_txpwr(),
            setter=lambda v: None,  # Applied by rcfg_radio
            min_val=5,
            max_val=23,
            config_key="lora.tx_power",
            staged=True,
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
        "rcfg_radio",
        "rssi",
        "savecfg",
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
        # param commands - pass radio_state for pending value access
        ("getcmds", partial(_handle_getcmds, cmd_names), CommandScope.ANY, False, False),
        ("getparam", partial(_handle_getparam, params, radio_state), CommandScope.ANY, False, False),
        ("getparams", partial(_handle_getparams, params, radio_state), CommandScope.ANY, False, False),
        # rcfg_radio - apply staged radio params; early_ack so ACK sent before apply
        ("rcfg_radio", partial(_handle_rcfg_radio, radio_state), CommandScope.PRIVATE, True, False),
        # rssi - return RSSI of the command packet; late_ack to include RSSI in response
        ("rssi", partial(_handle_rssi, radio_state), CommandScope.ANY, False, False),
        # savecfg - persist params to config file
        ("savecfg", partial(_handle_savecfg, params, config_path), CommandScope.PRIVATE, False, False),
        # setparam - late_ack to get error response; staged params applied by rcfg_radio
        ("setparam", partial(_handle_setparam, params, radio_state), CommandScope.PRIVATE, False, False),
    ]

    # Register all commands
    for name, handler, scope, early_ack, ack_jitter in commands:
        registry.register(name, handler, scope, early_ack=early_ack, ack_jitter=ack_jitter)

    logger.info(f"Registered {len(commands)} command handlers")
