# Data Log

Sensor data collection and LoRa radio communication for Raspberry Pi.

## Architecture

```
[Sensor Node]  --LoRa-->  [Gateway]  <--TCP-->  [Pi5 Dashboard]
   (Pi Zero)              (Pi Zero)              (rpi_server_cockpit)
```

- **Sensor Node** (`node_broadcast.py`): Reads sensors, broadcasts via LoRa
- **Gateway** (`gateway_server.py`): Receives LoRa, serves TCP clients (default: port 5001)

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
- `tcp_port`: Gateway TCP port (default 5001)
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

## Systemd Services

The `services/` folder contains systemd service files for running components on boot:

- `gateway_server.service` - Gateway that receives LoRa data and posts to dashboard
- `node_broadcast.service` - Node broadcaster that sends sensor readings via LoRa
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
./service_mod.sh --install gateway_server
./service_mod.sh --install node_broadcast
```

**Uninstall a service:**
```bash
./service_mod.sh --uninstall gateway_server
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
