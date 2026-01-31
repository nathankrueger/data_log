# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Raspberry Pi-based distributed sensor network with LoRa radio communication. Outdoor sensor nodes (Pi Zero 2W) broadcast readings via LoRa to indoor gateways, which POST data to a Pi 5 dashboard.

```
[Outdoor Node] --LoRa--> [Gateway] --HTTP--> [Pi 5 Dashboard]
  Pi Zero 2W              Pi Zero 2W           /api/timeseries/ingest
```

## Commands

```bash
# Setup
source install.sh              # Create/activate venv and install deps
./install.sh --reinstall       # Full reinstall

# Tests
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
- `SIGUSR2` - Disable LED flash-on-receive

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

## Configuration

JSON config files in `config/`:
- `node_config.json` - Sensors, broadcast interval, LoRa params
- `gateway_config.json` - Dashboard URL, local sensors, LED settings

See `.example` files for templates. Actual config files are gitignored.

## Adding New Components

**New Sensor:** Create class in `sensors/` extending `Sensor`, add to `SENSOR_CLASS_IDS` in `sensors/__init__.py`

**New Radio:** Create class in `radio/` extending `Radio`, export in `radio/__init__.py`

## Development Workflow

Development happens on a separate machine (not the Pi Zero 2W targets). The SSH/VSCode server setup on the Pi is too slow for practical development.

- **Do not attempt to run code or tests locally** - the hardware dependencies (GPIO, I2C sensors, LoRa radio) won't be available
- Files must be transferred to the Pi Zero 2W for testing
- The user will run tests manually on the target device

## Important

When adding dependencies or imports to python files, be sure to always add it to requirements.txt. Note that transitive dependencies (packages already required by other packages) don't need to be added explicitly.