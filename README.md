# Data Log

Sensor data collection and LoRa radio communication for Raspberry Pi.

## Architecture

```
[Sensor Node]  --LoRa-->  [Gateway]  --HTTP-->  [Server Dashboard]
   (Pi Zero)   <--cmd--   (Pi Zero)  <--POST--       (Pi 5)
```

- **Sensor Node** (`node_broadcast.py`): Reads sensors, broadcasts via LoRa, receives commands
- **Gateway** (`gateway_server.py`): Receives LoRa sensor data, posts to dashboard, forwards commands to nodes

## Setup

```bash
chmod +x install.sh
source install.sh
```

## Configuration

Copy example configs and edit for your setup:
```bash
cp config/node_config.json.example config/node_config.json
cp config/gateway_config.json.example config/gateway_config.json
```

Key settings:
- `node_id`: Unique identifier for this device
- `sensors`: List of sensor classes to read
- `dashboard_url`: Gateway's target dashboard URL (e.g., `http://192.168.1.100:5000`)
- `lora`: Radio frequency, pins, etc.

## Running

**Sensor Node:**
```bash
./scripts/launch_node_broadcast.sh
```

**Gateway:**
```bash
./scripts/launch_gateway_server.sh
```

## Gateway Commands (Gateway → Node)

The gateway can send commands to nodes over LoRa with ACK-based reliable delivery.

### Sending Commands

Send commands via HTTP POST to the gateway:
```bash
curl -X POST http://gateway:5001/command \
  -H "Content-Type: application/json" \
  -d '{"cmd": "ping", "node_id": "patio"}'

# Broadcast to all nodes (omit node_id)
curl -X POST http://gateway:5001/command \
  -d '{"cmd": "ping"}'

# With arguments
curl -X POST http://gateway:5001/command \
  -d '{"cmd": "set_interval", "args": ["30"], "node_id": "patio"}'
```

### Command Timing Configuration

LoRa is half-duplex (can't TX and RX simultaneously), so timing parameters control reliability:

**Gateway** (`gateway_config.json`):
```json
"command_server": {
    "enabled": true,
    "port": 5001,
    "initial_retry_ms": 1000,
    "max_retry_ms": 5000,
    "max_retries": 10
}
```

**Node** (`node_config.json`):
```json
"command_receiver": {
    "enabled": true,
    "receive_timeout": 0.5
}
```

| Parameter | Location | Description |
|-----------|----------|-------------|
| `initial_retry_ms` | Gateway | Time to wait for ACK before first retry |
| `max_retry_ms` | Gateway | Maximum retry delay (backoff cap) |
| `max_retries` | Gateway | Attempts before giving up |
| `receive_timeout` | Node | How long each receive window stays open |

**Tuning tips:**
- For non-idempotent commands (reboot, capture photo), use `initial_retry_ms >= 1000` to avoid duplicate execution
- `initial_retry_ms` should exceed `receive_timeout` + ~200ms for reliable first-attempt delivery
- First send is immediate; retry delays only affect recovery from lost packets

### Runtime LED Control

The gateway can flash an RGB LED when LoRa messages are received. Configure in `gateway_config.json` under the `led` section, including the default state via `flash_on_recv`.

Toggle at runtime without restarting:
```bash
sudo systemctl kill --signal=SIGUSR1 gateway.service  # Enable
sudo systemctl kill --signal=SIGUSR2 gateway.service  # Disable
```

## Systemd Services

The `services/` folder contains systemd service files for running components on boot:

- `gateway.service` - Gateway that receives LoRa data and posts to dashboard
- `node.service` - Node broadcaster that sends sensor readings via LoRa
- `data_log.service` - CSV logger service
- `radio_transmit.service` - Radio temperature sender

### Managing Services with service_mod.sh

Use the `service_mod.sh` script to easily manage services:

**List all services and their status:**
```bash
./service_mod.sh --list
```

**Install a service:**
```bash
./service_mod.sh --install gateway
./service_mod.sh --install node
```

**Uninstall a service:**
```bash
./service_mod.sh --uninstall gateway
```

**Get help:**
```bash
./service_mod.sh --help
```

The script automatically handles copying, enabling, starting, stopping, and removing services.

### Useful Service Commands

- Check status: `sudo systemctl status data_log.service`
- View logs: `journalctl -u data_log.service`
- Stop service: `sudo systemctl stop data_log.service`
- Start service: `sudo systemctl start data_log.service`
- Restart service: `sudo systemctl restart data_log.service`
- Disable on boot: `sudo systemctl disable data_log.service`
- Refresh service: `systemctl daemon-reload`

### RGB LED at Boot

If using an RGB LED (see `utils/led.py`), the LED may glow dimly at boot before the service initializes the GPIO pins. To ensure the LED is off at boot, add the following to `/boot/firmware/config.txt`:

```
# Set RGB LED pins to output, default high (for common anode LED = off)
gpio=17,22,27=op,dh
```

Adjust the pin numbers (BCM) to match your wiring. For a common cathode LED, use `dl` instead of `dh`.
