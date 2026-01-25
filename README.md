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

## Systemd Service (CSV Logger)

The `data_log.service` runs the CSV logger on boot if so configured:

```bash
sudo cp data_log.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable data_log.service
sudo systemctl start data_log.service
```

### Useful Service Commands

- Check status: `sudo systemctl status data_log.service`
- View logs: `journalctl -u data_log.service`
- Stop service: `sudo systemctl stop data_log.service`
- Start service: `sudo systemctl start data_log.service`
- Restart service: `sudo systemctl restart data_log.service`
- Disable on boot: `sudo systemctl disable data_log.service`
- Refresh service: `systemctl daemon-reload`
