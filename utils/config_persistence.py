"""
Utilities for persisting runtime configuration changes to JSON config files.

Provides atomic file writes to prevent corruption on power loss or crash.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# Serializes read-modify-write cycles: several HTTP threads may persist
# different keys at once, and an unlocked interleave would drop one update.
_write_lock = threading.Lock()


def update_config_file(
    config_path: str,
    updates: dict[str, Any],
) -> bool:
    """Thread-safe atomic update of keys in a JSON config file (details below)."""
    with _write_lock:
        return _update_config_file(config_path, updates)


def _update_config_file(
    config_path: str,
    updates: dict[str, Any],
) -> bool:
    """
    Update specific keys in a JSON config file atomically.

    Reads the existing config, merges the updates, and writes back atomically
    (temp file + fsync + rename + directory fsync), so a power cut leaves
    either the old or the new file, never a truncated one. The previous
    version is kept as <file>.bak, which load_json_with_backup() falls back to.

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

    try:
        raw = path.read_bytes()
        try:
            config = json.loads(raw)
            original_ok = True
        except (json.JSONDecodeError, UnicodeDecodeError):
            config = load_json_with_backup(path)  # raises if the backup is bad too
            original_ok = False

        # Apply updates using dot notation for nested keys
        for key_path, value in updates.items():
            _set_nested(config, key_path, value)
        data = (json.dumps(config, indent=4) + "\n").encode("utf-8")

        if original_ok:  # never replace a good backup with a corrupt file
            _atomic_write(_backup_path(path), raw)
        _atomic_write(path, data)
        return True
    except Exception as e:  # noqa: BLE001 - callers treat False as "not persisted"
        _logger.error(f"Could not update {path}: {e}")
        return False


BACKUP_SUFFIX = ".bak"


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def load_json_with_backup(config_path) -> Any:
    """
    Load a JSON file; if it is unreadable as JSON (e.g. truncated by a power
    cut), fall back to its .bak copy. Raises the original error if neither
    works. FileNotFoundError propagates unchanged.
    """
    path = Path(config_path)
    try:
        return json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        bak = _backup_path(path)
        try:
            data = json.loads(bak.read_bytes())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            raise e from None
        _logger.error(f"{path} is unreadable ({e}); using backup {bak}")
        return data


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _fsync_dir(dir_path: Path) -> None:
    """Make the rename itself durable (best effort; not all filesystems allow it)."""
    try:
        fd = os.open(dir_path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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
