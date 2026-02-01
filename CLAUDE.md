# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Raspberry Pi-based distributed sensor network with LoRa radio communication. Outdoor sensor nodes (Pi Zero 2W) broadcast readings via LoRa to indoor gateways, which POST data to a Pi 5 dashboard.

```
[Outdoor Node] --LoRa--> [Gateway] --HTTP--> [Pi 5 Dashboard]
  Pi Zero 2W              Pi Zero 2W           /api/timeseries/ingest
```

## Commands

**IMPORTANT:** Always activate the virtual environment before running any Python commands:
```bash
source .venv/bin/activate
```

```bash
# Setup
source install.sh              # Create/activate venv and install deps
./install.sh --reinstall       # Full reinstall

# Tests (venv must be active)
./run_tests.sh                 # Run pytest
pytest tests/ -v               # Verbose test output

# Run services manually
./scripts/launch_node_broadcast.sh [config_file]
./scripts/launch_gateway_server.sh [config_file]

# Systemd service management
./scripts/service_mod.sh --install node_broadcast
./scripts/service_mod.sh --list
./scripts/service_mod.sh --follow gateway_server
```

## Architecture

### Abstract Base Classes
- `sensors/base.py` - `Sensor` ABC with `init()`, `read()`, `get_names()`, `get_units()`
- `radio/base.py` - `Radio` ABC with `init()`, `send()`, `receive()`, `close()`

Both support context managers (`with` statement).

### Runtime Discovery
Sensors are discovered via reflection in `sensors/__init__.py`:
- `get_all_sensor_classes()` - Returns all Sensor subclasses
- `get_sensor_class(name)` - Lookup by class name string
- `SENSOR_CLASS_IDS` / `SENSOR_ID_CLASSES` - Compact IDs for protocol

This enables config-driven instantiation without hardcoding sensor types.

### Protocol (`utils/protocol.py`)
- CRC32 validation on all LoRa packets
- JSON message format with deterministic serialization
- Auto-splits large readings across multiple packets (LORA_MAX_PAYLOAD limit)
- Sensor ID format: `{node_id}_{sensor_class}_{reading_name}` (lowercase)

### Threading
- `LoRaReceiver` - Daemon thread for receiving packets
- `LocalSensorReader` - Daemon thread for periodic sensor reads
- LED flashing uses generation tracking to prevent race conditions

### Signal Handling (gateway_server.py)
- `SIGUSR1` - Enable LED flash-on-receive
- `SIGUSR2` - Disable LED flash-on-receive and turn off display

## Hardware

### Supported Sensors
| Sensor | Interface | Library |
|--------|-----------|---------|
| BME280 | I2C (SMBus 0/1) | pimoroni-bme280 |
| MMA8452 | I2C (addr 0x1D) | Manual register impl |
| Arducam IMX477 | Camera | picamera2, opencv (WIP) |

### Radio Modules
| Module | Notes |
|--------|-------|
| RFM9x | Adafruit, CS=GPIO24, RST=GPIO25 |

### RGB LED
- gpiozero library, configurable pins (default: R=17, G=27, B=22)
- Supports common anode/cathode
- Boot state configured in `/boot/firmware/config.txt`

### OLED Display (SSD1306)
- 128x64 pixel I2C display, default address 0x3C
- Uses `luma.oled` library
- Microswitch on GPIO16 cycles through display pages

## Configuration

JSON config files in `config/`:
- `node_config.json` - Sensors, broadcast interval, LoRa params
- `gateway_config.json` - Dashboard URL, local sensors, LED, display settings

See `.example` files for templates. Actual config files are gitignored.

Display config in `gateway_config.json`:
```json
"display": {
    "enabled": true,
    "advance_switch_pin": 16,
    "scroll_switch_pin": 26,
    "i2c_port": 1,
    "i2c_address": 60,
    "refresh_interval": 0.5
}
```
Both switch pins are optional - the system works without any buttons configured.

## Adding New Components

**New Sensor:** Create class in `sensors/` extending `Sensor`, add to `SENSOR_CLASS_IDS` in `sensors/__init__.py`

**New Radio:** Create class in `radio/` extending `Radio`, export in `radio/__init__.py`

**New Display Page:** Create class in `utils/display.py` extending `ScreenPage`, implement `get_lines() -> list[str | None]`, add instance to `pages` list in `gateway_server.py`

### Display System Architecture

```
utils/gateway_state.py    - GatewayState, LastPacketInfo (shared runtime state)
utils/display.py          - Display ABC, SSD1306Display, ScreenPage ABC, ScreenManager
gateway_server.py         - Creates display, pages, ScreenManager, and GPIO buttons
```

- `Display` ABC abstracts hardware (width, height, line_height, show, hide, clear, render_lines)
- `SSD1306Display` is the concrete implementation for 128x64 OLED
- `GatewayState` holds thread-safe runtime state (start time, last packet info)
- `ScreenPage` ABC defines `get_lines() -> list[str | None]` (any number of lines)
- If all lines are `None`, screen turns off
- `ScreenManager` handles display refresh (500ms), page cycling, and line scrolling
- GPIO buttons are set up externally in gateway_server.py using `advance_page()` and `scroll_page()`
- Built-in pages: `OffPage`, `SystemInfoPage`, `LastPacketPage`, `GatewayLocalSensors`

## Development Workflow

Development happens on a separate machine (not the Pi Zero 2W targets). The SSH/VSCode server setup on the Pi is too slow for practical development.

- **Do not attempt to run code or tests locally** - the hardware dependencies (GPIO, I2C sensors, LoRa radio) won't be available
- Files must be transferred to the Pi Zero 2W for testing
- The user will run tests manually on the target device

### Publishing to Target Hardware

Use the `/publish` skill to deploy code to Pi Zero 2W devices (commit, push, run `./publish.sh`).

Git credentials must be stored in `~/.git-credentials` for non-interactive push to work.

## Important

When adding dependencies or imports to python files, be sure to always add it to requirements.txt. Note that transitive dependencies (packages already required by other packages) don't need to be added explicitly.