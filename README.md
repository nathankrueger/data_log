# Data Logger

## Setup

1. Make the install script executable and run it:
```bash
chmod +x install.sh
source install.sh
```

## Systemd Service

To run the data logger automatically on boot:

1. Copy the service file:
```bash
sudo cp data_log.service /etc/systemd/system/
```

2. Reload systemd and enable the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable data_log.service
```

3. Start the service:
```bash
sudo systemctl start data_log.service
```

### Useful Commands

- Check status: `sudo systemctl status data_log.service`
- View logs: `journalctl -u data_log.service`
- Stop service: `sudo systemctl stop data_log.service`
- Start service: `sudo systemctl start data_log.service`
- Restart service: `sudo systemctl restart data_log.service`
- Disable on boot: `sudo systemctl disable data_log.service`
- Refresh service: `systemctl daemon-reload`
