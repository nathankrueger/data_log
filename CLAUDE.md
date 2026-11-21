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
│   ├── sensor_collection.py   # SensorDataCollector (+ live feed/nodes/catalog), DashboardClient, LocalSensorReader
│   ├── params.py              # Gateway parameter registry
│   ├── http_handler.py        # Compat shim -> gateway.web (CommandServer = WebServer)
│   ├── web/                   # HTTP server: command API + browser UI
│   │   ├── server.py          # WebServer thread (ThreadingHTTPServer, HTTP/1.1), WebContext, router assembly
│   │   ├── core.py            # Request/Response/StreamResponse/Router/HTTPError
│   │   ├── static_files.py    # Cached static serving (ETag, gzip)
│   │   ├── routes_commands.py # /command, /discover, /gateway/*, /{cmd}, /{cmd}/{node} (registered LAST)
│   │   ├── routes_live.py     # /api/status, /api/readings, /api/feed, /api/params
│   │   ├── routes_logger.py   # /api/logger/*
│   │   ├── routes_pages.py    # /, /logger, /static/*, /favicon.ico
│   │   └── static/            # index.html, logger.html, css/, js/ (ES modules), vendor/ (uPlot)
│   └── timeseries/            # Opt-in history logger (SQLite rollups, CSV export)
│       ├── service.py         # LoggerService: charts config, recording on/off, queries
│       ├── recorder.py        # Recorder thread: buffered ingest, rollup accumulators, prune
│       ├── store.py           # TimeSeriesStore: raw + agg tables, tier selection
│       └── export.py          # Streamed CSV (wide / long)
├── node/                       # Outdoor node package
│   └── data_log.py            # Sensor broadcasting, CommandReceiver
├── utils/                      # Shared utilities
│   ├── protocol.py            # LoRa packet encoding/decoding
│   ├── command_registry.py    # Command handler registration
│   └── config_persistence.py  # Atomic config file updates
├── radio/                      # Radio hardware abstraction
├── sensors/                    # Sensor hardware abstraction (+ lora_link.py: gateway RSSI/SNR)
├── display/                    # OLED display base classes and hardware
├── scripts/                    # Shell scripts and tools
│   ├── set_radio_params.sh    # Change SF/BW across all nodes + gateway
│   └── ...
└── config/                     # JSON configuration files
```

## Architecture

### Abstract Base Classes
- `sensors/base.py` - `Sensor` ABC with `init()`, `read()`, `get_names()`, `get_units()`
- `radio/base.py` - `Radio` ABC with `init()`, `send()`, `receive()`, `close()`, `idle()`, `recover_rx()`, `hard_reset()`

Both support context managers (`with` statement).

### Runtime Discovery
Sensors are discovered via reflection in `sensors/__init__.py`:
- `get_all_sensor_classes()` - Returns all Sensor subclasses
- `get_sensor_class(name)` - Lookup by class name string
- `SENSOR_CLASS_IDS` / `SENSOR_ID_CLASSES` - Manually assigned compact IDs for protocol (never reassign)

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
- `gateway/web/routes_commands.py` - HTTP command API (POST /command, gateway param endpoints)
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

**RX recovery:** The SX1276 LoRa modem can enter a stuck state where `rx_done` is permanently asserted (reads stale FIFO data — 7 bytes of 0xFF). `CommandReceiver` tracks consecutive invalid packets on G2N and calls `hard_reset()` after 10 in a row. `hard_reset()` toggles the hardware reset pin and re-initializes all registers from cached values (SF, BW, freq, TX power). The `reset_radio` command allows triggering this on demand. `RFM9xRadio` caches SF/BW/freq/txpwr so `hard_reset()` never reads corrupted registers.

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

### Web UI and HTTP Server (`gateway/web/`)

One in-process `ThreadingHTTPServer` (HTTP/1.1 keep-alive, thread per connection) serves both the command API and the browser UI on `command_server.port` (5001). It must stay in-process: it shares the command queue, radio state and collector, and the radio has a single owner — don't move it to gunicorn/pre-fork servers. Thread-per-connection matters because command endpoints block for up to `wait_timeout` (and /discover much longer).

- Pages: `/` (Live: nodes, feed, params) and `/logger` (history charts). Static assets in `gateway/web/static/`; no build step, no CDN (works offline on the road). uPlot is vendored in `static/vendor/`.
- Route order matters: `routes_commands.register()` goes last because `GET /{cmd}` and `GET /{cmd}/{node}` are catch-alls. Names in `RESERVED_SEGMENTS` or containing `.` are never sent over LoRa (browser requests like `/favicon.ico` used to be broadcast as commands).
- Command API responses keep the historical JSON format (`json_response(..., compact=False)`); UI APIs use compact JSON.
- Feed: `SensorDataCollector` keeps a bounded deque of tuples with contiguous sequence numbers; `GET /api/readings?after=SEQ&epoch=E` returns only new entries. Size is set from the UI (`PUT /api/feed`), persisted as `web.feed_size`. A collapsed feed card isn't polled; its badge uses `feed.count` from `/api/status`. The time column shows dates only while a visible row is from before today (rollover or quiet nodes), re-checked every poll so midnight flips it.
- UI JS: build DOM with `h()` from `js/common.js` (text is never parsed as HTML — node/sensor names come off the radio).
- UI conventions in `js/common.js`: every scroll region gets edge fades (`.scroll` / `[data-fade-axis]` via `initScrollFades()`, or `scrollFade(el)` for regions built later; set `data-fade-sticky` for sticky headers; keep borders/backgrounds on a wrapper, not the masked scroller). Reorderable cards use `sortable()` + `dragHandle()` (items need `data-id`; renders must not reorder the DOM while `.active`). A drag gesture must always end: listeners live on `window`, a mouse move with no button down ends a missed release, blur/hidden cancels, and cleanup has a timeout fallback. `sortable(..., {rows: N})` is the row layout used by charts. Items flow N to a row, `data-new-row` starts a row, and `applyFlow()` marks a chart alone in its row `.wide` (it spans the grid). Drop zones are measured from the dragged card's center, not the pointer. A clear move up or down (`NEW_ROW_TRAVEL`, about 100 px for a full chart) into the band above or below a row gives the item its own row. Anything else places it beside a card in that row, so a sideways drag only reorders. `move()` (arrow keys, menu) swaps slots with a neighbour. Collapsible cards are `<details data-collapse-key>` via `initCollapsibles()`. Chart cards instead use a chevron button with per-browser `chart.<id>.collapsed`, and nothing is fetched while a chart is collapsed. `showMenu()` items may be checkboxes (`checked` + `onToggle`, the menu stays open). Node card order persists as `web.node_order`. Chart order, row starts (`new_row`) and `auto_range` persist in `logger.json`.
- Preview without hardware: `python scripts/web_demo.py [--backfill-hours 72]` → http://localhost:5002/

### History Logger (`gateway/timeseries/`)

Opt-in, for watching battery loggers away from home (at home the Pi 5 dashboard stores everything). Users create **charts** (1–8 series sharing units + a max age). A series is recorded while it is on any chart; its retention is the longest max age of those charts. One on/off switch, persisted in `<data_dir>/logger.json`; data in `<data_dir>/timeseries.db` (default `data/logger/`, gitignored).

- **Lean when off:** `sqlite3`, the store and the Recorder thread are only loaded when recording starts; the collector's listener hook is `None`.
- **Storage:** `raw(sid, ts_ms, v)` and `agg(sid, tier, ts, n, vmin, vmax, vsum)`, both `WITHOUT ROWID` clustered on `(sid, [tier,] ts)`. Tiers: 60/300/1800/10800 s.
- **Ingest:** `Recorder.ingest()` only appends to a buffer (no I/O on the LoRa thread). The recorder thread flushes every `logger.flush_interval_s` (30 s) in one transaction, updating in-memory per-(series, tier) accumulators — O(tiers) per sample, never re-scanning raw rows. Unflushed samples are merged into raw-tier queries so live charts aren't delayed.
- **Query:** raw if the window holds ≤ 2×points samples (checked with LIMIT), else the finest tier that fits. O(log n + points) regardless of history length. Incremental polling passes `tier` + `since`.
- **Auto range** (`auto=1`, per chart, on by default): the window start moves up to the oldest stored or buffered sample. That is one index seek per series. The window keeps at least `AUTO_MIN_SPAN_S`, and the resolution is picked for the clipped window. The response's `first` lets the browser keep the same window between polls.
- **Timestamps** are the gateway's receive time (one clock for all nodes; node clocks may be wrong without NTP on the road).
- **Retention** pruning every `logger.prune_interval_s`: index range deletes. Freed pages are reused; `incremental_vacuum` only after big deletes.
- **Size:** "Stored" is `store.used_bytes()`, (page_count − freelist) × page_size measured after each write, not file sizes: every commit appends whole 4 KB pages to the WAL. `wal_autocheckpoint` is 100 pages (~400 KB, default ~4 MB) and `journal_size_limit` trims the WAL after big transactions. The storage API also returns `disk_bytes`.
- CSV export streams (chunked) via a k-way merge of per-series cursors: wide (one column per series) or long.

### Reliability Rules (gateway must run unattended for a year)

- **No thread may die silently.** Every loop body catches `Exception` and keeps going; anything that must stay up has a watchdog in the `run_gateway()` main loop (every 30 s: `LoggerService.check_health()` restarts a dead or corrupt-DB recorder with exponential backoff).
- **Everything keyed by radio input is bounded:** collector nodes/series (`MAX_NODES`/`MAX_SERIES`, least-recently-heard evicted), feed deque, recorder buffer (`MAX_PENDING`, newest kept), HTTP connections (`MAX_CONNECTIONS`, excess closed), request bodies (1 MB), series/time range per logger query.
- **UI/logging bookkeeping never blocks the dashboard POST path:** `add_readings()` isolates live-state and listener failures.
- **Self-healing storage:** a damaged `timeseries.db` (SQLITE_CORRUPT/NOTADB, see `store.is_corruption`) is moved aside as `*.corrupt-<stamp>` and recording continues on a fresh file; an unparseable `logger.json` is moved aside, and malformed chart entries are repaired on load. Nothing is ever deleted automatically.
- **Web server never gives up:** bind failures retry with backoff, and an accept-loop failure rebuilds the server.
- **Config writes are serialized** (`utils/config_persistence` holds a lock) because HTTP handlers now run concurrently.
- **The radio must stay healthy or the process restarts.**
  - **Hard resets:** the transceiver hard-resets the SX1276 after any of these:
    - 10 unreadable packets in a row (garbage or instant CRC drops).
    - 3 failed transmissions in a row.
    - No valid packet for `lora.silence_reset_s`. The default is 900 s, backing off to 4 h; 0 disables it.
  - **Recovery mode:** any exception in the transceiver loop puts it into recovery mode. Recovery mode rebuilds the radio stack in place every 3 s until it works (`RFM9xRadio.reconnect()`, e.g. FT232H unplug/replug), then retunes to N2G. The web UI and logger stay up meanwhile, and the heartbeat keeps beating.
  - **Restart:** the main loop checks transceiver liveness and its heartbeat (120 s). If either fails, it exits with code 1, and systemd or `scripts/launch_gateway_server.sh` restarts the gateway. The launch script supervises itself when it isn't running under systemd.
  - **Boot:** a radio that fails at boot also exits 1, unless `lora.required` is false.
- **Timers use `time.monotonic()`** (command retries, ACK waits, response TTLs, discovery). Wall-clock time is only for data timestamps. History pruning is held for 24 h after a forward wall-clock jump, so a bogus clock can't wipe history.
- **Exactly one recorder may write** the history DB: `TimeSeriesStore.set_owner()` fences out a recorder that outlived its stop; reader connections are never re-pooled across a writer close/quarantine.
- **Config files are written durably** (fsync + rename + dir fsync) with a `.bak` of the previous version; `load_config()` falls back to it if the file is unreadable.
- Process lock files live in `~/.cache/data_log/` (not `/tmp`, which periodic cleaners may purge).
- Tests for these live in `tests/test_reliability.py` (concurrency, fault injection, recovery) and `tests/test_gateway_runtime.py` (command queue timing/bounds, transceiver radio health with a scripted fake radio, packet validation). The key invariant: after any interleaving of ingest, crashes, retries and purges, every stored rollup bucket equals what the surviving raw rows imply.

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

### Gateway local sensors and per-node RSSI/SNR logging

Each `local_sensors` entry may set its own `interval_sec`; entries without one use `local_sensor_interval_sec`. `LocalSensorReader` (`gateway/sensor_collection.py`) schedules each entry independently. It sleeps on a `threading.Event` until the next timed entry is due or until a packet arrives for an every-packet entry.

LoRa link quality is logged with two gateway-only "sensors" in `sensors/lora_link.py`, configured like any other local sensor:

```json
"local_sensors": [
    {"class": "LoRaRSSI", "interval_sec": 0,   "config": {"nodes": ["ab01-7", "ab01-8"]}},
    {"class": "LoRaSNR",  "interval_sec": 600, "config": {"nodes": ["ab01-7"]}}
]
```

- **`nodes` is required and is the only way a node gets logged.** There is deliberately no "all nodes" mode. An entry without `nodes` fails to initialize and is skipped with an error. To give different nodes different rates, use several entries of the same class.
- **`interval_sec: 0` (`EVERY_PACKET`)** logs every packet received from the node. This value is valid only for link sensors; hardware sensors fall back to the default interval with a warning.
- **`interval_sec: N`** logs at most one value per node every N seconds: the most recently received one. Nothing is logged if no packet arrived since the last publish. The effective rate is therefore min(interval, packet rate), and asking for a faster rate than the node transmits never logs repeats.
- Each value is timestamped with the packet's **receive time**, not the publish time.
- Values are posted **under the node they describe**, not the gateway (`collector.add_readings(node_id, ..., is_local=False, measured=True)`).
- `measured=True` means the readings are not a packet from the node. They update its series at the packet's receive time, but leave the node's "last heard" time, packet count and interval alone. They are recorded to history at their own timestamps. They land on the dashboard as `ab01_7_lorarssi_rssi` / `ab01_7_lorasnr_snr` ("ab01-7 RSSI" in dBm, "ab01-7 SNR" in dB), category "Remote Sensors". The dashboard needs no changes because ingest registers new series automatically.
- **Data flow:**
  1. `LoRaTransceiver._process_received_packet()` reads `get_last_rssi()`/`get_last_snr()`.
  2. For each valid sensor packet (ACKs and packets that fail the CRC are excluded), it calls `record_packet()` on each link sensor. Link sensors ignore nodes they don't list.
  3. `LocalSensorReader` then calls `drain()`.
- Both radio values are cached by `adafruit_rfm9x` during `receive()`, so reading them never touches SPI (see "SPI Contention" above).
- `server.py` instantiates local sensors **before** LoRa init, so the transceiver can be handed the link sensors. The reader thread still starts afterwards.
- Link sensors are fed by the transceiver, not polled: `read()` returns `()`. They need no `_SENSOR_ID_MAP` entry because they never travel over LoRa.
- `sensors/lora_link.py` imports `utils.protocol` lazily, because the repo-root package `__init__` imports `sensors` before `utils` is importable.
- Tests: `tests/test_lora_link.py`.

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

Web UI / history logger in `gateway_config.json` (all optional):
```json
"web": { "feed_size": 200 },
"logger": { "data_dir": "data/logger", "flush_interval_s": 30, "prune_interval_s": 300 }
```
Charts and the recording switch are managed from the UI and live in `<data_dir>/logger.json`, not the gateway config.

## Adding New Components

**New Sensor:** Create class in `sensors/` extending `Sensor`, add to `_SENSOR_ID_MAP` in `sensors/__init__.py` with the next available ID

**New Radio:** Create class in `radio/` extending `Radio`, export in `radio/__init__.py`

**New Display Page:** Create class extending `ScreenPage` in the appropriate package (`gateway/display_pages.py` or `node/display_pages.py`), implement `get_lines() -> list[str | None]`, add instance to `pages` list in the corresponding server module

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

When adding dependencies or imports to python files, be sure to add them to the correct requirements file. Portable packages (no hardware dependency) go in `requirements.txt`. Raspberry Pi hardware packages (GPIO, I2C, SPI, display drivers) go in `requirements-hw.txt`. Transitive dependencies don't need to be added explicitly.