"""
Display pages for gateway.

Provides ScreenPage implementations specific to gateways:
- SystemInfoPage: Shows system information (IP, uptime, last packet, dashboard)
- LastPacketPage: Shows details of last received LoRa packet
- GatewayLocalSensors: Shows local sensor readings
"""

import time
from datetime import datetime
from urllib.parse import urlparse

from .display import ScreenPage, _format_duration, _get_ip_address
from .gateway_state import GatewayState


class SystemInfoPage(ScreenPage):
    """
    System information page.

    Shows:
    - Header
    - IP address
    - Uptime
    - Time since last packet
    - Dashboard IP
    """

    def __init__(self, state: GatewayState):
        self._state = state

    def get_lines(self) -> list[str | None]:
        ip = _get_ip_address()
        uptime = _format_duration(time.time() - self._state.start_time)

        last_pkt = self._state.get_last_packet()
        if last_pkt.timestamp > 0:
            ago = _format_duration(time.time() - last_pkt.timestamp)
            last_pkt_str = f"{ago} ago"
        else:
            last_pkt_str = "Never"

        # Extract host from dashboard URL
        dashboard_ip = "N/A"
        if self._state.dashboard_url:
            parsed = urlparse(self._state.dashboard_url)
            dashboard_ip = parsed.hostname or "N/A"

        return [
            "System Information",
            f"IP: {ip}",
            f"Uptime: {uptime}",
            f"Last pkt: {last_pkt_str}",
            f"Dashbrd: {dashboard_ip}",
        ]


class LastPacketPage(ScreenPage):
    """
    Last packet details page.

    Shows:
    - Header [RSSI]
    - Timestamp
    - Sensor name
    - Sensor value
    """

    def __init__(self, state: GatewayState):
        self._state = state

    def get_lines(self) -> list[str | None]:
        last_pkt = self._state.get_last_packet()

        if last_pkt.timestamp == 0:
            return [
                "Last Packet",
                "---",
                "No packets yet",
                None,
            ]

        ts = datetime.fromtimestamp(last_pkt.timestamp)
        time_str = ts.strftime("%H:%M:%S")

        # Truncate long names to fit display
        name = last_pkt.sensor_name[:16]
        node = last_pkt.node_id[:16]
        rssi = last_pkt.rssi
        value_str = f"{last_pkt.sensor_value:.1f} {last_pkt.sensor_units}"

        return [
            f"Last Packet [RSSI: {rssi}]",
            f"time: {time_str}",
            f"name: {node}:{name}",
            f"val: {value_str}",
        ]


class GatewayLocalSensors(ScreenPage):
    """
    Gateway local sensors page.

    Shows:
    - Header
    - Up to 3 local sensor readings (name: value units)
    - Shows "---" for missing sensor slots
    """

    def __init__(self, state: GatewayState):
        self._state = state

    def get_lines(self) -> list[str | None]:
        sensors = self._state.get_local_sensors()

        lines: list[str | None] = ["Local Sensors"]

        # Show up to 3 sensors (we have 4 lines, 1 for header)
        for i in range(3):
            if i < len(sensors):
                s = sensors[i]
                # Truncate name to fit, leave room for value
                name = s.name[:8]
                lines.append(f"{name}: {s.value:.1f} {s.units}")
            else:
                lines.append("---")

        return lines
