#!/usr/bin/env python3
"""
Radio bandwidth test tool - similar to iperf3 for radio modules.

Works with any radio implementation that inherits from Radio base class.

Usage:
    python3 radio_bandwidth.py server              # Run on receiver Pi
    python3 radio_bandwidth.py client              # Run on transmitter Pi
    python3 radio_bandwidth.py client -t 30        # Test for 30 seconds
    python3 radio_bandwidth.py client -n 100       # Send 100 packets
    python3 radio_bandwidth.py client --radio rfm9x  # Specify radio type
"""

import argparse
import struct
import sys
import time

from radio import Radio, RFM9xRadio

# Protocol constants
PACKET_SIZE = 252  # Max payload for most LoRa radios
MAGIC_START = b"BW"
MAGIC_END = b"EN"
MAGIC_ACK = b"AK"


def format_bytes(bytes_val: int) -> str:
    """Format bytes into human readable string."""
    if bytes_val >= 1_000_000:
        return f"{bytes_val / 1_000_000:.2f} MB"
    elif bytes_val >= 1_000:
        return f"{bytes_val / 1_000:.2f} KB"
    else:
        return f"{bytes_val} B"


def format_rate(bytes_per_sec: float) -> str:
    """Format transfer rate into human readable string."""
    if bytes_per_sec >= 1_000_000:
        return f"{bytes_per_sec / 1_000_000:.2f} MB/s"
    elif bytes_per_sec >= 1_000:
        return f"{bytes_per_sec / 1_000:.2f} KB/s"
    else:
        return f"{bytes_per_sec:.2f} B/s"




def create_radio(radio_type: str) -> Radio:
    """
    Create a radio instance based on type string.

    Args:
        radio_type: Radio type identifier (e.g., 'rfm9x')

    Returns:
        Configured Radio instance (not yet initialized)
    """
    if radio_type == "rfm9x":
        return RFM9xRadio(
            frequency_mhz=915.0,
            tx_power=23,
            cs_pin=24,
            reset_pin=25,
        )
    else:
        raise ValueError(f"Unknown radio type: {radio_type}")


def run_server(radio: Radio) -> None:
    """Run bandwidth test server (receiver)."""
    print("-" * 60)
    print("Server listening...")
    print("-" * 60)

    while True:
        # Wait for start packet
        print("\nWaiting for client connection...")
        packet = radio.receive(timeout=None)

        if packet and packet[:2] == MAGIC_START:
            num_packets = struct.unpack(">I", packet[2:6])[0]
            print(f"Client connected, expecting {num_packets} packets")
            print("-" * 60)
            print(f"{'Interval':<15} {'Transfer':<15} {'Rate':<15} {'RSSI':<10}")
            print("-" * 60)

            # Receive data packets
            packets_received = 0
            bytes_received = 0
            start_time = time.time()
            interval_start = start_time
            interval_bytes = 0
            rssi_sum = 0

            while True:
                packet = radio.receive(timeout=5.0)

                if packet is None:
                    continue

                if packet[:2] == MAGIC_END:
                    break

                packets_received += 1
                bytes_received += len(packet)
                interval_bytes += len(packet)
                rssi = radio.get_last_rssi()
                if rssi:
                    rssi_sum += rssi

                # Print interval stats every second
                now = time.time()
                if now - interval_start >= 1.0:
                    elapsed = now - start_time
                    interval_rate = interval_bytes / (now - interval_start)
                    avg_rssi = rssi_sum / packets_received if packets_received > 0 else 0

                    interval_str = f"{elapsed - 1:.1f}-{elapsed:.1f} sec"
                    print(f"{interval_str:<15} {format_bytes(interval_bytes):<15} {format_rate(interval_rate):<15} {avg_rssi:.0f} dBm")

                    interval_start = now
                    interval_bytes = 0

            # Final stats
            end_time = time.time()
            total_time = end_time - start_time
            avg_rate = bytes_received / total_time if total_time > 0 else 0
            packet_loss = ((num_packets - packets_received) / num_packets * 100) if num_packets > 0 else 0
            avg_rssi = rssi_sum / packets_received if packets_received > 0 else 0

            print("-" * 60)
            print("Test Complete")
            print("-" * 60)
            print(f"Duration:        {total_time:.2f} seconds")
            print(f"Transfer:        {format_bytes(bytes_received)}")
            print(f"Rate:         {format_rate(avg_rate)}")
            print(f"Packets:         {packets_received}/{num_packets} ({packet_loss:.1f}% loss)")
            print(f"Avg RSSI:        {avg_rssi:.0f} dBm")
            print("-" * 60)

            # Send ACK
            radio.send(MAGIC_ACK + struct.pack(">I", packets_received))


def run_client(radio: Radio, duration: int = 10, num_packets: int | None = None) -> None:
    """Run bandwidth test client (sender)."""
    # Calculate number of packets
    if num_packets is None:
        # Estimate packets based on duration (rough estimate: ~5 packets/sec for LoRa)
        num_packets = duration * 10

    print("-" * 60)
    print("Connecting to server...")
    print(f"Test duration: {duration} seconds, packets: {num_packets}")
    print("-" * 60)

    # Send start packet
    start_packet = MAGIC_START + struct.pack(">I", num_packets)
    radio.send(start_packet)
    time.sleep(0.1)

    # Generate test data (fill packet with incrementing bytes)
    test_data = bytes([i % 256 for i in range(PACKET_SIZE)])

    print(f"{'Interval':<15} {'Transfer':<15} {'Rate':<15}")
    print("-" * 60)

    # Send data packets
    packets_sent = 0
    bytes_sent = 0
    start_time = time.time()
    interval_start = start_time
    interval_bytes = 0

    while packets_sent < num_packets:
        # Send packet with sequence number
        seq_bytes = struct.pack(">I", packets_sent)
        packet = seq_bytes + test_data[:PACKET_SIZE - 4]

        radio.send(packet)
        packets_sent += 1
        bytes_sent += len(packet)
        interval_bytes += len(packet)

        # Print interval stats every second
        now = time.time()
        if now - interval_start >= 1.0:
            elapsed = now - start_time
            interval_rate = interval_bytes / (now - interval_start)

            interval_str = f"{elapsed - 1:.1f}-{elapsed:.1f} sec"
            print(f"{interval_str:<15} {format_bytes(interval_bytes):<15} {format_rate(interval_rate):<15}")

            interval_start = now
            interval_bytes = 0

        # Check if we've exceeded duration
        if time.time() - start_time >= duration:
            break

    # Send end packet
    time.sleep(0.2)
    radio.send(MAGIC_END)

    end_time = time.time()
    total_time = end_time - start_time
    avg_rate = bytes_sent / total_time if total_time > 0 else 0

    # Wait for ACK
    print("\nWaiting for server report...")
    ack = radio.receive(timeout=5.0)
    packets_received = 0
    if ack and ack[:2] == MAGIC_ACK:
        packets_received = struct.unpack(">I", ack[2:6])[0]

    packet_loss = ((packets_sent - packets_received) / packets_sent * 100) if packets_sent > 0 else 0

    print("-" * 60)
    print("Test Complete")
    print("-" * 60)
    print(f"Duration:        {total_time:.2f} seconds")
    print(f"Transfer:        {format_bytes(bytes_sent)}")
    print(f"Rate:         {format_rate(avg_rate)}")
    print(f"Packets sent:    {packets_sent}")
    if packets_received > 0:
        print(f"Packets recv:    {packets_received} ({packet_loss:.1f}% loss)")
    print("-" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Radio bandwidth test tool (like iperf3 for radios)"
    )
    parser.add_argument("mode", choices=["server", "client", "s", "c"], help="Run as server (receiver) or client (sender)")
    parser.add_argument("-t", "--time", type=int, default=60, help="Test duration in seconds (default: 60)")
    parser.add_argument("-n", "--num", type=int, default=None, help="Number of packets to send")
    parser.add_argument("-r", "--radio", type=str, default="rfm9x", choices=["rfm9x"], help="Radio type to use (default: rfm9x)")
    args = parser.parse_args()

    radio = create_radio(args.radio)

    try:
        radio.init()
        print(f"Radio initialized: {args.radio}")

        if args.mode in ["server", "s"]:
            run_server(radio)
        else:
            run_client(radio, duration=args.time, num_packets=args.num)
    except KeyboardInterrupt:
        print("\nTest interrupted")
    finally:
        radio.close()


if __name__ == "__main__":
    main()
