#!/usr/bin/env python3
"""
LoRa range test tool.

Sends periodic ping commands to a CubeCell node and logs responses with
RSSI, GPS coordinates, and timestamps.  For field-testing signal range.

The node (range_test.ino on HTCC-AB01) listens on G2N (915.5 MHz) for
ping commands, then replies with an ACK and a sensor packet containing
GPS coordinates on N2G (915.0 MHz).

Usage:
    python3 range_test.py                       # Defaults: ping ab01 every 5s
    python3 range_test.py --node ab01 -i 10     # Ping every 10 seconds
    python3 range_test.py --csv results.csv     # Also log to CSV file
    python3 range_test.py --duration 300        # Run for 5 minutes
    python3 range_test.py --tx-power 5          # Low power for short-range test
"""

import argparse
import csv
import json
import signal
import sys
import tempfile
import time
import zlib
from datetime import datetime
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radio import RFM9xRadio
from utils.protocol import build_command_packet, parse_ack_packet

# ── Dual-channel frequencies (must match Arduino sketch) ─────────────────

FREQ_N2G = 915.0   # Node-to-Gateway: sensor data + ACKs
FREQ_G2N = 915.5   # Gateway-to-Node: commands


def set_frequency(radio: RFM9xRadio, freq_mhz: float) -> None:
    """Switch the radio frequency at runtime."""
    radio.set_frequency(freq_mhz)


def parse_sensor_packet(data: bytes) -> dict | None:
    """Parse a sensor packet from the range test node.

    Verifies CRC and extracts GPS readings by key name.
    Returns dict with node_id, latitude, longitude, satellites, node_rssi
    or None if invalid.
    """
    try:
        text = data.decode("utf-8").lstrip()  # strip ASR650x TX-FIFO padding
        message = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if "r" not in message or "n" not in message:
        return None

    # Verify CRC
    crc_field = message.get("c", "")
    msg_no_crc = {k: v for k, v in message.items() if k != "c"}
    json_str = json.dumps(msg_no_crc, sort_keys=True, separators=(",", ":"))
    computed = f"{zlib.crc32(json_str.encode('utf-8')) & 0xFFFFFFFF:08x}"
    if computed != crc_field:
        return None

    result: dict = {"node_id": message["n"]}
    for r in message.get("r", []):
        key = r.get("k", "")
        value = r.get("v")
        if key == "alt":
            result["altitude"] = value
        elif key == "lat":
            result["latitude"] = value
        elif key == "lng":
            result["longitude"] = value
        elif key == "sats":
            result["satellites"] = value
        elif key == "rssi":
            result["node_rssi"] = value

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LoRa range test — sends pings and logs responses "
                    "with RSSI and GPS coordinates"
    )
    parser.add_argument(
        "--node", "-n", default="ab01",
        help="Target node ID (default: ab01)",
    )
    parser.add_argument(
        "--interval", "-i", type=float, default=5.0,
        help="Seconds between pings (default: 5.0)",
    )
    parser.add_argument(
        "--duration", "-d", type=float, default=None,
        help="Total test duration in seconds (default: unlimited)",
    )
    parser.add_argument(
        "--csv", "-o", type=str, default=None,
        help="Path to CSV output file (default: stdout only)",
    )
    parser.add_argument(
        "--rx-timeout", type=float, default=3.0,
        help="Seconds to wait for node response after ping (default: 3.0)",
    )
    parser.add_argument(
        "--tx-power", type=int, default=23,
        help="Transmit power in dBm, 5-23 (default: 23)",
    )
    parser.add_argument(
        "--cs-pin", type=int, default=24,
        help="GPIO pin for radio chip select (default: 24)",
    )
    parser.add_argument(
        "--reset-pin", type=int, default=25,
        help="GPIO pin for radio reset (default: 25)",
    )
    parser.add_argument(
        "--map", action="store_true", default=False,
        help="Generate an HTML map after the test",
    )
    return parser.parse_args()


def run_range_test(args: argparse.Namespace) -> None:
    radio = RFM9xRadio(
        frequency_mhz=FREQ_N2G,
        tx_power=args.tx_power,
        cs_pin=args.cs_pin,
        reset_pin=args.reset_pin,
    )
    radio.init()

    csv_header = [
        "timestamp", "seq", "gateway_rssi", "node_rssi",
        "latitude", "longitude", "altitude", "satellites",
        "ack", "round_trip_ms",
    ]
    csv_rows = []

    csv_writer = None
    csv_file = None
    if args.csv:
        csv_file = open(args.csv, "w", newline="")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(csv_header)

    # Header
    print(f"Range test → node={args.node}  interval={args.interval}s  "
          f"tx_power={args.tx_power}dBm")
    header = (
        f"{'Time':<12} {'Seq':>4} {'GW RSSI':>8} {'Node RSSI':>10} "
        f"{'Latitude':>11} {'Longitude':>12} {'Alt(m)':>8} {'Sats':>5} {'ACK':>4} {'RTT':>7}"
    )
    print(header)
    print("-" * len(header))

    seq = 0
    start_time = time.time()
    running = True

    def handle_sigint(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        while running:
            if args.duration and (time.time() - start_time) >= args.duration:
                break

            seq += 1
            ping_time = time.time()
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:12]

            # ── Send ping on G2N ──
            set_frequency(radio, FREQ_G2N)
            packet, command_id = build_command_packet("ping", [], args.node)
            radio.send(packet)

            # ── Listen on N2G for ACK + sensor data ──
            set_frequency(radio, FREQ_N2G)

            ack_received = False
            gw_rssi = None
            sensor_data = None
            deadline = time.time() + args.rx_timeout

            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                response = radio.receive(timeout=min(0.5, remaining))
                if response is None:
                    continue

                rssi = radio.get_last_rssi()

                # Try ACK first
                ack = parse_ack_packet(response)
                if ack and ack.command_id == command_id:
                    ack_received = True
                    gw_rssi = rssi
                    continue  # keep listening for sensor packet

                # Try sensor packet
                parsed = parse_sensor_packet(response)
                if parsed and parsed.get("node_id") == args.node:
                    sensor_data = parsed
                    if gw_rssi is None:
                        gw_rssi = rssi
                    break  # got everything we need

            # ── Format output ──
            round_trip_ms = (time.time() - ping_time) * 1000

            lat = sensor_data.get("latitude", "") if sensor_data else ""
            lon = sensor_data.get("longitude", "") if sensor_data else ""
            alt = sensor_data.get("altitude", "") if sensor_data else ""
            sats = sensor_data.get("satellites", "") if sensor_data else ""
            node_rssi = sensor_data.get("node_rssi", "") if sensor_data else ""

            # Sentinel: (0.0, 0.0) means no GPS fix
            if lat == 0.0 and lon == 0.0:
                lat = "no fix"
                lon = "no fix"

            ack_str = "Y" if ack_received else "N"
            gw_rssi_str = str(gw_rssi) if gw_rssi is not None else "--"
            node_rssi_str = str(int(node_rssi)) if node_rssi != "" else "--"
            alt_str = f"{alt:.1f}" if isinstance(alt, (int, float)) else "--"
            rtt_str = f"{round_trip_ms:.0f}ms"

            line = (
                f"{timestamp:<12} {seq:>4} {gw_rssi_str:>8} {node_rssi_str:>10} "
                f"{str(lat):>11} {str(lon):>12} {alt_str:>8} {str(sats):>5} "
                f"{ack_str:>4} {rtt_str:>7}"
            )
            print(line)

            row = [
                datetime.now().isoformat(), seq,
                gw_rssi if gw_rssi is not None else "",
                node_rssi, lat, lon, alt, sats,
                ack_received, f"{round_trip_ms:.0f}",
            ]
            csv_rows.append(row)

            if csv_writer:
                csv_writer.writerow(row)
                csv_file.flush()

            # Wait for next interval
            elapsed = time.time() - ping_time
            if elapsed < args.interval:
                time.sleep(args.interval - elapsed)

    finally:
        radio.close()
        if csv_file:
            csv_file.close()
            print(f"\nResults saved to {args.csv}")

        print(f"\n--- Range Test Summary ---")
        print(f"Pings sent: {seq}")
        duration = time.time() - start_time
        print(f"Duration: {duration:.1f}s")

    if args.map:
        try:
            from range_map import generate_map_from_csv

            if args.csv:
                csv_for_map = args.csv
                html_path = str(Path(args.csv).with_suffix(".html"))
            else:
                # Write accumulated rows to a temp file
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".csv", delete=False, newline="",
                )
                writer = csv.writer(tmp)
                writer.writerow(csv_header)
                writer.writerows(csv_rows)
                tmp.close()
                csv_for_map = tmp.name
                html_path = f"range_test_{args.node}.html"

            map_path = generate_map_from_csv(
                csv_path=csv_for_map,
                output_path=html_path,
                title=f"Range Test - {args.node}",
            )
            print(f"Map saved to {map_path}")
        except ImportError:
            print("Warning: Could not import range_map (is folium installed?)")
        except Exception as e:
            print(f"Warning: Map generation failed: {e}")


if __name__ == "__main__":
    args = parse_args()
    run_range_test(args)
