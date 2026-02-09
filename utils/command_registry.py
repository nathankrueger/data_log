"""
Command registry for node-side command handling.

Provides a registry pattern for mapping command names to callbacks,
with scope filtering to distinguish between broadcast and targeted commands.

Follows the HTCC AB01 earlyAck pattern:
- early_ack=True (default): ACK is sent before the handler runs
- early_ack=False: Handler runs first, ACK is sent after with optional response payload
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)


class CommandScope(Enum):
    """Scope filter for command callbacks."""

    BROADCAST = "broadcast"  # Only respond to broadcast commands (node_id="")
    PRIVATE = "private"  # Only respond to targeted commands (node_id=self)
    ANY = "any"  # Respond to both broadcast and targeted commands


# Callback signature: (command_name, args) -> optional response payload
CommandCallback = Callable[[str, list[str]], dict | None]


@dataclass
class HandlerEntry:
    """A registered command handler with its configuration."""

    callback: CommandCallback
    scope: CommandScope
    early_ack: bool
    ack_jitter: bool


class CommandRegistry:
    """
    Registry for command handlers with scope filtering and earlyAck support.

    Handlers are registered with a command name, scope, and early_ack flag.

    - early_ack=True: ACK sent before handler (for fire-and-forget commands)
    - early_ack=False: Handler runs first, ACK sent after with response payload
      (for commands that return data like echo, params)

    Example:
        registry = CommandRegistry("node_001")

        def handle_ping(cmd: str, args: list[str]):
            print("pong")

        def handle_echo(cmd: str, args: list[str]) -> dict | None:
            return {"data": args[0]} if args else None

        # ACK before handler (default)
        registry.register("ping", handle_ping, CommandScope.ANY, early_ack=True)

        # Handler before ACK (returns data in ACK payload)
        registry.register("echo", handle_echo, CommandScope.PRIVATE, early_ack=False)
    """

    def __init__(self, node_id: str):
        """
        Initialize the registry.

        Args:
            node_id: This node's identifier for matching targeted commands.
        """
        self.node_id = node_id
        self._handlers: dict[str, list[HandlerEntry]] = {}

    def register(
        self,
        command: str,
        callback: CommandCallback,
        scope: CommandScope = CommandScope.ANY,
        early_ack: bool = True,
        ack_jitter: bool = False,
    ) -> None:
        """
        Register a callback for a command.

        Multiple callbacks can be registered for the same command.
        Each callback can have a different scope.

        Args:
            command: Command name to handle (e.g., "reboot", "set_interval")
            callback: Function to call when command is received
            scope: When to invoke: BROADCAST, PRIVATE, or ANY
            early_ack: True = ACK before handler, False = ACK after handler with response
            ack_jitter: True = add random delay before sending ACK (for discovery)
        """
        if command not in self._handlers:
            self._handlers[command] = []
        entry = HandlerEntry(
            callback=callback, scope=scope, early_ack=early_ack, ack_jitter=ack_jitter
        )
        self._handlers[command].append(entry)
        logger.debug(
            f"Registered handler for '{command}' "
            f"(scope={scope.value}, early_ack={early_ack}, ack_jitter={ack_jitter})"
        )

    def unregister(self, command: str, callback: CommandCallback) -> bool:
        """
        Unregister a specific callback for a command.

        Args:
            command: Command name
            callback: The callback to remove

        Returns:
            True if callback was found and removed, False otherwise
        """
        if command not in self._handlers:
            return False

        original_len = len(self._handlers[command])
        self._handlers[command] = [
            entry for entry in self._handlers[command] if entry.callback != callback
        ]

        if not self._handlers[command]:
            del self._handlers[command]

        return len(self._handlers.get(command, [])) < original_len

    def lookup(
        self, command: str, target_node_id: str
    ) -> HandlerEntry | None:
        """
        Look up the first matching handler for a command.

        Used by CommandReceiver to check the early_ack flag before deciding
        whether to send ACK before or after dispatch.

        Args:
            command: Command name
            target_node_id: Target node from packet ("" for broadcast)

        Returns:
            First matching HandlerEntry, or None if no match
        """
        if command not in self._handlers:
            return None

        is_broadcast = target_node_id == ""
        is_for_me = target_node_id == self.node_id

        if not is_broadcast and not is_for_me:
            return None

        for entry in self._handlers[command]:
            should_match = False
            if entry.scope == CommandScope.ANY:
                should_match = True
            elif entry.scope == CommandScope.BROADCAST and is_broadcast:
                should_match = True
            elif entry.scope == CommandScope.PRIVATE and is_for_me:
                should_match = True

            if should_match:
                return entry

        return None

    def dispatch(
        self, command: str, args: list[str], target_node_id: str
    ) -> tuple[bool, dict | None]:
        """
        Dispatch a command to registered handlers.

        Filters handlers based on:
        1. Command name must match
        2. Scope must match:
           - BROADCAST: only if target_node_id is empty
           - PRIVATE: only if target_node_id matches self.node_id
           - ANY: always matches (if command matches and is for this node)

        Commands targeted to other nodes are ignored entirely.

        Args:
            command: Command name received
            args: Command arguments
            target_node_id: Target node from packet ("" for broadcast)

        Returns:
            Tuple of (handled, response_payload):
            - handled: True if at least one handler was invoked
            - response_payload: First non-None response from a handler, for ACK payload
        """
        if command not in self._handlers:
            logger.debug(f"No handlers registered for command '{command}'")
            return False, None

        is_broadcast = target_node_id == ""
        is_for_me = target_node_id == self.node_id

        # Ignore commands targeted to other nodes
        if not is_broadcast and not is_for_me:
            logger.debug(
                f"Ignoring command '{command}' targeted to '{target_node_id}'"
            )
            return False, None

        handled = False
        response: dict | None = None
        for entry in self._handlers[command]:
            should_invoke = False

            if entry.scope == CommandScope.ANY:
                should_invoke = True
            elif entry.scope == CommandScope.BROADCAST and is_broadcast:
                should_invoke = True
            elif entry.scope == CommandScope.PRIVATE and is_for_me:
                should_invoke = True

            if should_invoke:
                try:
                    result = entry.callback(command, args)
                    handled = True
                    if response is None and result is not None:
                        response = result
                    logger.debug(
                        f"Executed handler for '{command}' "
                        f"(scope={entry.scope.value}, broadcast={is_broadcast})"
                    )
                except Exception as e:
                    logger.error(f"Handler for '{command}' raised exception: {e}")

        return handled, response

    def get_registered_commands(self) -> list[str]:
        """Return list of all registered command names."""
        return list(self._handlers.keys())
