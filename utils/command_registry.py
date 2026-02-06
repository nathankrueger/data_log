"""
Command registry for node-side command handling.

Provides a registry pattern for mapping command names to callbacks,
with scope filtering to distinguish between broadcast and targeted commands.
"""

import logging
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)


class CommandScope(Enum):
    """Scope filter for command callbacks."""

    BROADCAST = "broadcast"  # Only respond to broadcast commands (node_id="")
    PRIVATE = "private"  # Only respond to targeted commands (node_id=self)
    ANY = "any"  # Respond to both broadcast and targeted commands


# Callback signature: (command_name, args) -> None
CommandCallback = Callable[[str, list[str]], None]


class CommandRegistry:
    """
    Registry for command handlers with scope filtering.

    Handlers are registered with a command name and scope. When a command
    is dispatched, only handlers matching both the command name and scope
    criteria are invoked.

    Example:
        registry = CommandRegistry("node_001")

        def handle_reboot(cmd: str, args: list[str]):
            os.system("sudo reboot")

        # Only respond to targeted reboot commands
        registry.register("reboot", handle_reboot, CommandScope.PRIVATE)

        # This will invoke the callback (targeted to this node)
        registry.dispatch("reboot", [], "node_001")

        # This will NOT invoke the callback (broadcast)
        registry.dispatch("reboot", [], "")
    """

    def __init__(self, node_id: str):
        """
        Initialize the registry.

        Args:
            node_id: This node's identifier for matching targeted commands.
        """
        self.node_id = node_id
        self._handlers: dict[str, list[tuple[CommandCallback, CommandScope]]] = {}

    def register(
        self,
        command: str,
        callback: CommandCallback,
        scope: CommandScope = CommandScope.ANY,
    ) -> None:
        """
        Register a callback for a command.

        Multiple callbacks can be registered for the same command.
        Each callback can have a different scope.

        Args:
            command: Command name to handle (e.g., "reboot", "set_interval")
            callback: Function to call when command is received
            scope: When to invoke: BROADCAST, PRIVATE, or ANY
        """
        if command not in self._handlers:
            self._handlers[command] = []
        self._handlers[command].append((callback, scope))
        logger.debug(f"Registered handler for '{command}' with scope {scope.value}")

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
            (cb, scope) for cb, scope in self._handlers[command] if cb != callback
        ]

        if not self._handlers[command]:
            del self._handlers[command]

        return len(self._handlers.get(command, [])) < original_len

    def dispatch(self, command: str, args: list[str], target_node_id: str) -> bool:
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
            True if at least one handler was invoked, False otherwise
        """
        if command not in self._handlers:
            logger.debug(f"No handlers registered for command '{command}'")
            return False

        is_broadcast = target_node_id == ""
        is_for_me = target_node_id == self.node_id

        # Ignore commands targeted to other nodes
        if not is_broadcast and not is_for_me:
            logger.debug(
                f"Ignoring command '{command}' targeted to '{target_node_id}'"
            )
            return False

        handled = False
        for callback, scope in self._handlers[command]:
            should_invoke = False

            if scope == CommandScope.ANY:
                should_invoke = True
            elif scope == CommandScope.BROADCAST and is_broadcast:
                should_invoke = True
            elif scope == CommandScope.PRIVATE and is_for_me:
                should_invoke = True

            if should_invoke:
                try:
                    callback(command, args)
                    handled = True
                    logger.debug(
                        f"Executed handler for '{command}' "
                        f"(scope={scope.value}, broadcast={is_broadcast})"
                    )
                except Exception as e:
                    logger.error(f"Handler for '{command}' raised exception: {e}")

        return handled

    def get_registered_commands(self) -> list[str]:
        """Return list of all registered command names."""
        return list(self._handlers.keys())
