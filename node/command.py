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
import subprocess
import time
from functools import partial
from typing import TYPE_CHECKING

from utils.command_registry import CommandRegistry, CommandScope
from utils.led import parse_color, scale_brightness
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


def _handle_uptime(start_time: float, _cmd: str, args: list[str]) -> dict:
    """
    Handle uptime command - returns seconds since node started.

    Uses early_ack=false so uptime is included in response payload.
    Returns {"r": uptime_seconds} as integer.
    """
    uptime = int(time.time() - start_time)
    logger.info(f"[HANDLER] Uptime: {uptime}s")
    return {"r": uptime}


def _handle_reset(_cmd: str, args: list[str]) -> None:
    """
    Handle reset command - restart the node service.

    Spawns a detached process that waits 1 second (to allow ACK to be sent)
    then restarts the node.service via systemctl.

    Uses early_ack=True so ACK is sent before restart begins.
    """
    logger.info("[HANDLER] Reset command received - restarting node.service in 1s")
    subprocess.Popen(
        ["sh", "-c", "sleep 1 && sudo systemctl restart node.service"],
        start_new_session=True,
    )


def _handle_blink(state: NodeState, _cmd: str, args: list[str]) -> None:
    """
    Handle blink command - sets LED to a color for a duration (non-blocking).

    Args:
        args[0]: Color name (required) - "red"/"r", "green"/"g", "blue"/"b",
                 "yellow"/"y", "cyan"/"c", "magenta"/"m", "white"/"w", "off"/"o"
        args[1]: Duration in seconds (optional, default 0.5)
        args[2]: Brightness 0-255 (optional, default from config)

    Uses RgbLed.flash() for non-blocking operation.
    Uses early_ack=True so ACK is sent before flash starts.
    """
    if state.led is None:
        logger.warning("[HANDLER] blink: LED not available")
        return

    if not args:
        logger.warning("[HANDLER] blink: missing color argument")
        return

    # Parse color
    rgb = parse_color(args[0])
    if rgb is None:
        logger.warning(f"[HANDLER] blink: unrecognized color '{args[0]}'")
        return

    # Parse optional duration (default 0.5s)
    duration = 0.5
    if len(args) >= 2:
        try:
            duration = float(args[1])
            if duration <= 0:
                logger.warning("[HANDLER] blink: duration must be positive")
                return
        except ValueError:
            logger.warning(f"[HANDLER] blink: invalid duration '{args[1]}'")
            return

    # Parse optional brightness (default from config)
    brightness = state.default_brightness
    if len(args) >= 3:
        try:
            brightness = int(args[2])
            if not 0 <= brightness <= 255:
                logger.warning("[HANDLER] blink: brightness must be 0-255")
                return
        except ValueError:
            logger.warning(f"[HANDLER] blink: invalid brightness '{args[2]}'")
            return

    # Scale RGB by brightness and flash
    scaled = scale_brightness(rgb, brightness)
    logger.info(
        f"[HANDLER] blink: color={args[0]} duration={duration}s brightness={brightness}"
    )
    state.led.flash(scaled[0], scaled[1], scaled[2], duration)


def _handle_testled(state: NodeState, _cmd: str, args: list[str]) -> None:
    """
    Handle testled command - cycles through all colors for diagnostic testing.

    Args:
        args[0]: Delay in milliseconds per color (optional, default 5000)
        args[1]: Brightness 0-255 (optional, default from config)

    This is a BLOCKING command - takes ~50 seconds with default timing
    (7 colors + 3 full-brightness primaries at 5s each).

    Uses early_ack=True so ACK is sent before the test starts.
    """
    if state.led is None:
        logger.warning("[HANDLER] testled: LED not available")
        return

    # Parse optional delay (default 5000ms)
    delay_ms = 5000
    if args:
        try:
            delay_ms = int(args[0])
            if delay_ms <= 0:
                delay_ms = 5000
        except ValueError:
            pass

    # Parse optional brightness (default from config)
    brightness = state.default_brightness
    if len(args) >= 2:
        try:
            brightness = int(args[1])
            if not 0 <= brightness <= 255:
                brightness = state.default_brightness
        except ValueError:
            pass

    delay_sec = delay_ms / 1000.0
    logger.info(
        f"[HANDLER] testled: cycling colors, {delay_ms}ms per step, brightness={brightness}"
    )

    # Color test sequence (matches AB01)
    colors = [
        ("red", (255, 0, 0)),
        ("green", (0, 255, 0)),
        ("blue", (0, 0, 255)),
        ("yellow", (255, 255, 0)),
        ("cyan", (0, 255, 255)),
        ("magenta", (255, 0, 255)),
        ("white", (255, 255, 255)),
    ]

    for name, rgb in colors:
        scaled = scale_brightness(rgb, brightness)
        logger.info(f"[HANDLER] testled: {name}")
        state.led.set_rgb(scaled[0], scaled[1], scaled[2])
        time.sleep(delay_sec)

    # Full-brightness primary test (matches AB01)
    logger.info("[HANDLER] testled: full-brightness RGB test")
    for name, rgb in [("red", (255, 0, 0)), ("green", (0, 255, 0)), ("blue", (0, 0, 255))]:
        logger.info(f"[HANDLER] testled: {name} 255")
        state.led.set_rgb(rgb[0], rgb[1], rgb[2])
        time.sleep(delay_sec)

    state.led.off()
    logger.info("[HANDLER] testled: complete")


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
        "blink",
        "discover",
        "echo",
        "getcmds",
        "getparam",
        "getparams",
        "ping",
        "rcfg_radio",
        "reset",
        "rssi",
        "savecfg",
        "setparam",
        "testled",
        "uptime",
    ])

    # ─── Command Table ───────────────────────────────────────────────────────
    # Format: (name, handler, scope, early_ack, ack_jitter)
    #
    # early_ack=True:  ACK sent before handler runs (fire-and-forget)
    # early_ack=False: Handler runs first, ACK sent after with response payload
    #
    # ack_jitter=True: Random delay before ACK (for broadcast discovery)
    commands = [
        # blink - set LED color for duration (non-blocking)
        ("blink", partial(_handle_blink, state), CommandScope.ANY, True, False),
        # ping - responds to both broadcast and private
        ("ping", _handle_ping, CommandScope.ANY, True, False),
        # discover - broadcast with jitter to avoid ACK collisions
        ("discover", _handle_ping, CommandScope.BROADCAST, True, True),
        # echo - returns response payload (ANY scope for broadcast support)
        ("echo", _handle_echo, CommandScope.ANY, False, False),
        # param commands - pass radio_state for pending value access
        ("getcmds", partial(_handle_getcmds, cmd_names), CommandScope.ANY, False, False),
        ("getparam", partial(_handle_getparam, params, radio_state), CommandScope.ANY, False, False),
        ("getparams", partial(_handle_getparams, params, radio_state), CommandScope.ANY, False, False),
        # rcfg_radio - apply staged radio params; early_ack so ACK sent before apply
        ("rcfg_radio", partial(_handle_rcfg_radio, radio_state), CommandScope.PRIVATE, True, False),
        # reset - restart node service; PRIVATE to prevent accidental broadcast reset
        ("reset", _handle_reset, CommandScope.PRIVATE, True, False),
        # rssi - return RSSI of the command packet; late_ack to include RSSI in response
        ("rssi", partial(_handle_rssi, radio_state), CommandScope.ANY, False, False),
        # savecfg - persist params to config file
        ("savecfg", partial(_handle_savecfg, params, config_path), CommandScope.PRIVATE, False, False),
        # setparam - late_ack to get error response; staged params applied by rcfg_radio
        ("setparam", partial(_handle_setparam, params, radio_state), CommandScope.PRIVATE, False, False),
        # testled - cycle through colors (blocking); early_ack so ACK sent before test starts
        ("testled", partial(_handle_testled, state), CommandScope.ANY, True, False),
        # uptime - returns seconds since node started; late_ack to include uptime in response
        ("uptime", partial(_handle_uptime, state.start_time), CommandScope.ANY, False, False),
    ]

    # Register all commands
    for name, handler, scope, early_ack, ack_jitter in commands:
        registry.register(name, handler, scope, early_ack=early_ack, ack_jitter=ack_jitter)

    logger.info(f"Registered {len(commands)} command handlers")
