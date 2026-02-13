# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Raspberry Pi-based distributed sensor network with LoRa radio communication. Outdoor sensor nodes (Pi Zero 2W) broadcast readings via LoRa to indoor gateways, which POST data to a Pi 5 dashboard.

```
[Outdoor Node] --LoRa--> [Gateway] --HTTP--> [Pi 5 Dashboard]
  Pi Zero 2W   <--cmd--  Pi Zero 2W  <--POST--  (commands)
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
./scripts/service_mod.sh --install node
./scripts/service_mod.sh --list
./scripts/service_mod.sh --follow gateway
```

## Project Structure

```
data_log/
├── gateway/                    # Indoor gateway package
│   ├── server.py              # Main orchestration, run_gateway(), main()
│   ├── command_queue.py       # CommandQueue, PendingCommand, DiscoveryRequest
│   ├── transceiver.py         # LoRaTransceiver thread
│   ├── sensor_collection.py   # SensorDataCollector, DashboardClient, LocalSensorReader
│   ├── http_handler.py        # HTTP server, gateway param endpoints
│   └── params.py              # Gateway parameter registry
├── node/                       # Outdoor node package
│   └── data_log.py            # Sensor broadcasting, CommandReceiver
├── utils/                      # Shared utilities
│   ├── protocol.py            # LoRa packet encoding/decoding
│   ├── command_registry.py    # Command handler registration
│   └── config_persistence.py  # Atomic config file updates
├── radio/                      # Radio hardware abstraction
├── sensors/                    # Sensor hardware abstraction
├── display/                    # OLED display pages
├── scripts/                    # Shell scripts and tools
│   ├── set_radio_params.sh    # Change SF/BW across all nodes + gateway
│   └── ...
└── config/                     # JSON configuration files
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
- `LoRaTransceiver` - Gateway daemon thread for receiving packets and sending commands
- `CommandReceiver` - Node daemon thread for receiving commands
- `LocalSensorReader` - Daemon thread for periodic sensor reads
- LED flashing uses generation tracking to prevent race conditions

### LoRa Command System (Gateway → Node)

Bidirectional command system with ACK-based reliability:

```
Dashboard --HTTP POST--> Gateway --LoRa--> Node
                              <--ACK--
```

**Key files:**
- `utils/protocol.py` - `build_command_packet()`, `parse_command_packet()`, `build_ack_packet()`, `parse_ack_packet()`
- `gateway/http_handler.py` - HTTP server (POST /command, gateway param endpoints)
- `utils/command_registry.py` - `CommandRegistry`, `CommandScope`, `HandlerEntry`
- `gateway/command_queue.py` - `CommandQueue`, `PendingCommand`, `DiscoveryRequest`
- `gateway/transceiver.py` - `LoRaTransceiver`
- `node/data_log.py` - `CommandReceiver`

**Packet types:**
- Command: `{"t":"cmd", "n":"node_id", "cmd":"ping", "a":[], "ts":1699999999, "c":"crc32"}`
- ACK: `{"t":"ack", "id":"1699999999_a1b2", "n":"node_id", "c":"crc32"}`
- ACK with payload: `{"t":"ack", "id":"...", "n":"node_id", "p":{...}, "c":"crc32"}`
- Command ID format: `{timestamp}_{first 4 chars of CRC}`

**earlyAck pattern (matches HTCC AB01):**
- `early_ack=True` (default): ACK sent before handler runs (fire-and-forget commands like ping, blink)
- `early_ack=False`: Handler runs first, ACK sent after with response payload (echo, params)
- `CommandRegistry.lookup()` checks earlyAck flag, `CommandReceiver._process_packet()` uses it

#### LoRa Timing Parameters (Critical for Tuning)

The RFM9x is **half-duplex** - it cannot transmit and receive simultaneously. This creates timing constraints between gateway retries and node receive windows.

**Gateway side (`gateway_config.json` → `command_server`):**
| Parameter | Default | Description |
|-----------|---------|-------------|
| `initial_retry_ms` | 500 | Wait time before first retry if no ACK |
| `max_retry_ms` | 5000 | Maximum retry delay (backoff cap) |
| `max_retries` | 10 | Give up after this many attempts |

**Node side (`node_config.json` → `command_receiver`):**
| Parameter | Default | Description |
|-----------|---------|-------------|
| `receive_timeout` | 0.5 | Seconds each `receive()` call blocks |

**How timing works:**

1. Gateway sends command immediately when queued (no initial delay)
2. Node's `CommandReceiver` runs tight loop: `receive(timeout)` → process → send ACK
3. If no ACK arrives, gateway waits `initial_retry_ms` before retransmitting
4. Backoff doubles each retry: 300ms → 600ms → 1200ms → 2400ms... capped at `max_retry_ms`
5. When ACK arrives, command retires; next queued command sends immediately

**Why commands can be delayed:**
- Node is transmitting sensor data (holds `radio_lock`, blocking receive)
- Node's `receive()` started just after command arrived (worst case: full timeout delay before next receive window)
- ACK collided with gateway's retry transmission

**Tuning for non-idempotent commands** (e.g., reboot, capture_photo):
- Use longer `initial_retry_ms` (1000-2000ms) to avoid duplicate execution before ACK arrives
- Throughput is limited by round-trip time (~600-800ms), not retry delay
- First send is always immediate; retry delay only affects recovery from lost packets/ACKs

**Independence of parameters:**
- `initial_retry_ms` (gateway) and `receive_timeout` (node) are independent - they run on different devices
- However, `initial_retry_ms` should exceed node's worst-case response time: `receive_timeout` + processing + ACK TX (~100ms)
- For `receive_timeout=0.5`, use `initial_retry_ms >= 800` to avoid most unnecessary retries

**Node threading model:**
```
Main thread (broadcast_loop):
  while True:
    read sensors
    with radio_lock: radio.send(packets)
    sleep until next interval

CommandReceiver thread:
  while True:
    with radio_lock: packet = radio.receive(timeout=0.5)
    if packet: parse, send ACK, dispatch to handlers
```

The `radio_lock` ensures half-duplex safety. If broadcast loop is transmitting, CommandReceiver waits.

### Gateway Radio Parameter Access (SPI Contention)

**Problem:** The transceiver thread's tight `receive()` loop causes SPI lock starvation. Adafruit's SPIDevice uses a spinlock (`while not try_lock(): sleep(0)`). The transceiver releases and immediately reacquires the lock, starving the HTTP thread indefinitely.

**Solution:** RadioState caches sf/bw/txpwr at init. The cache is updated in `apply_pending()` (called only by transceiver thread) after writing to hardware. HTTP handlers read the cache, never touching SPI.

**CRITICAL:** Never modify radio params directly via `radio_state._radio`. Always use `set_pending()` + `apply_pending()` to keep cache in sync with hardware.

### Gateway Parameter Endpoints

The gateway exposes HTTP endpoints for runtime parameter tuning:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/gateway/params` | Get all gateway radio parameters |
| GET | `/gateway/param/{name}` | Get single parameter value |
| PUT | `/gateway/param/{name}` | Set parameter (JSON body: `{"value": X}`) |

**Available parameters:**
| Name | Type | Range | Writable |
|------|------|-------|----------|
| `sf` | int | 7-12 | Yes |
| `bw` | int | 0-2 (125/250/500 kHz) | Yes |
| `txpwr` | int | 5-23 dBm | Yes |
| `nodeid` | str | - | No |
| `n2g_freq` | float | MHz | No (restart required) |
| `g2n_freq` | float | MHz | No (restart required) |

Changes are persisted to `gateway_config.json` atomically.

### Coordinated Radio Parameter Changes

Use `scripts/set_radio_params.sh` to change SF/BW across all nodes and gateway:

```bash
./scripts/set_radio_params.sh --sf 9              # Change SF only
./scripts/set_radio_params.sh --bw 1              # BW: 0=125kHz, 1=250kHz, 2=500kHz
./scripts/set_radio_params.sh --sf 9 --bw 1       # Change both
./scripts/set_radio_params.sh --sf 9 --force      # Continue if some nodes fail
./scripts/set_radio_params.sh --sf 9 --dry-run    # Show what would change
```

The script:
1. Runs discovery 3 times to validate consistent node list
2. Updates each node via `setparam` command (aborts on failure unless `--force`)
3. Updates gateway LAST (to maintain communication during transition)

### Signal Handling (gateway/server.py)
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

**New Display Page:** Create class in `display/` extending `ScreenPage`, implement `get_lines() -> list[str | None]`, add instance to `pages` list in `gateway/server.py`

### Display System Architecture

```
utils/gateway_state.py    - GatewayState, LastPacketInfo (shared runtime state)
utils/display.py          - Display ABC, SSD1306Display, ScreenPage ABC, ScreenManager
gateway/server.py         - Creates display, pages, ScreenManager, and GPIO buttons
```

- `Display` ABC abstracts hardware (width, height, line_height, show, hide, clear, render_lines)
- `SSD1306Display` is the concrete implementation for 128x64 OLED
- `GatewayState` holds thread-safe runtime state (start time, last packet info)
- `ScreenPage` ABC defines `get_lines() -> list[str | None]` (any number of lines)
- If all lines are `None`, screen turns off
- `ScreenManager` handles display refresh (500ms), page cycling, and line scrolling
- GPIO buttons are set up externally in gateway/server.py using `advance_page()` and `scroll_page()`
- Built-in pages: `OffPage`, `SystemInfoPage`, `LastPacketPage`, `GatewayLocalSensors`

## Development Workflow

Development happens on a separate machine (not the Pi Zero 2W targets). The SSH/VSCode server setup on the Pi is too slow for practical development.

- **Do not attempt to run code or tests locally** - the hardware dependencies (GPIO, I2C sensors, LoRa radio) won't be available
- Files must be transferred to the Pi Zero 2W for testing
- The user will run tests manually on the target device

### HTCC AB01 Parity

When modifying node-side behavior (e.g., `node/data_log.py`, `CommandReceiver`, command handlers), ask the user if equivalent changes should be made to the HTCC AB01 codebase at `../htcc_ab01_datalog/data_log/data_log.ino`.

### Publishing to Target Hardware

Use the `/publish` skill to deploy code to Pi Zero 2W devices (commit, push, run `./publish.sh`).

Git credentials must be stored in `~/.git-credentials` for non-interactive push to work.

## Important

When adding dependencies or imports to python files, be sure to always add it to requirements.txt. Note that transitive dependencies (packages already required by other packages) don't need to be added explicitly.