"""
Utilities for persisting runtime configuration changes to JSON config files.

Provides atomic file writes to prevent corruption on power loss or crash.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def update_config_file(
    config_path: str,
    updates: dict[str, Any],
) -> bool:
    """
    Update specific keys in a JSON config file atomically.

    Reads the existing config, merges the updates, and writes back atomically
    using a temp file + rename pattern to prevent corruption.

    Args:
        config_path: Path to the JSON config file
        updates: Dict of key paths to values. Supports nested paths with dots:
                 {"lora.spreading_factor": 9} updates config["lora"]["spreading_factor"]

    Returns:
        True if successful, False otherwise

    Example:
        update_config_file("config/gateway_config.json", {
            "lora.spreading_factor": 9,
            "lora.signal_bandwidth": 250000,
        })
    """
    path = Path(config_path)

    if not path.exists():
        return False

    # Read existing config
    with open(path) as f:
        config = json.load(f)

    # Apply updates using dot notation for nested keys
    for key_path, value in updates.items():
        _set_nested(config, key_path, value)

    # Write atomically: write to temp file, then rename
    dir_path = path.parent
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=dir_path,
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(config, tmp, indent=4)
            tmp.write("\n")  # Trailing newline
            tmp_path = tmp.name

        # Atomic rename (on POSIX systems)
        os.replace(tmp_path, path)
        return True

    except (OSError, json.JSONDecodeError):
        # Clean up temp file if it exists
        if "tmp_path" in locals():
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def _set_nested(d: dict, key_path: str, value: Any) -> None:
    """
    Set a nested dict value using dot notation.

    Example:
        _set_nested(config, "lora.spreading_factor", 9)
        # Sets config["lora"]["spreading_factor"] = 9
    """
    keys = key_path.split(".")
    for key in keys[:-1]:
        if key not in d:
            d[key] = {}
        d = d[key]
    d[keys[-1]] = value


def get_nested(d: dict, key_path: str, default: Any = None) -> Any:
    """
    Get a nested dict value using dot notation.

    Example:
        get_nested(config, "lora.spreading_factor", 7)
        # Returns config["lora"]["spreading_factor"] or 7 if not found
    """
    keys = key_path.split(".")
    for key in keys:
        if not isinstance(d, dict) or key not in d:
            return default
        d = d[key]
    return d
