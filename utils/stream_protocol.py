"""
Stream protocol for arbitrary byte payloads over LoRa.

Designed to be implementable on Arduino/embedded C with minimal resources.
Supports multi-packet streaming with per-packet CRC16 and final CRC32.

Data packet format (max 250 bytes):
┌────────┬────────┬─────┬─────┬─────────────────┬───────┐
│ magic  │tot_len │ seq │ cnt │ payload         │ crc16 │
│ 2B     │ 4B     │ 2B  │ 2B  │ ≤238B           │ 2B    │
└────────┴────────┴─────┴─────┴─────────────────┴───────┘

- magic: 0xDA7A identifier for data packets
- tot_len: total payload size (all packets combined), max 16MB
- seq: packet sequence number (0-indexed)
- cnt: total packet count
- crc16: CRC16-CCITT over header + payload (for early rejection)

The final assembled payload has a CRC32 suffix for end-to-end verification.

Optional FEC (Forward Error Correction):
- Uses simple XOR parity: can recover ONE lost packet per block
- Parity packets use magic 0xDA7B
- Block size is configurable (default: 4 data packets + 1 parity)
- Minimal code footprint for Arduino implementation
"""

import struct
import zlib
from dataclasses import dataclass


# Protocol constants
MAGIC_DATA = 0xDA7A      # Data packet magic
MAGIC_PARITY = 0xDA7B    # Parity packet magic (for FEC)
MAGIC = MAGIC_DATA       # Alias for backward compatibility
HEADER_FMT = ">HIHH"     # magic(2) + total_len(4) + seq(2) + count(2) = 10 bytes
HEADER_SIZE = struct.calcsize(HEADER_FMT)
CRC16_SIZE = 2
CRC32_SIZE = 4
LORA_MAX_PACKET = 250
MAX_PAYLOAD_PER_PACKET = LORA_MAX_PACKET - HEADER_SIZE - CRC16_SIZE  # 238 bytes
DEFAULT_FEC_BLOCK_SIZE = 4  # Number of data packets per parity packet


def crc16_ccitt(data: bytes, initial: int = 0xFFFF) -> int:
    """
    CRC16-CCITT (XModem variant).

    Polynomial: 0x1021
    Initial: 0xFFFF

    This is easy to implement on Arduino and matches common libraries.
    """
    crc = initial
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def crc32(data: bytes) -> int:
    """CRC32 matching zlib.crc32 (already used in Arduino code)."""
    return zlib.crc32(data) & 0xFFFFFFFF


@dataclass
class StreamPacket:
    """A single packet in a multi-packet stream."""
    total_len: int
    seq: int
    count: int
    payload: bytes

    def __post_init__(self):
        if self.seq >= self.count:
            raise ValueError(f"seq {self.seq} >= count {self.count}")
        if self.count < 1:
            raise ValueError(f"count must be >= 1, got {self.count}")


class PackError(Exception):
    """Error during packing."""
    pass


class UnpackError(Exception):
    """Error during unpacking."""
    pass


def pack_stream(data: bytes) -> list[bytes]:
    """
    Pack arbitrary bytes into LoRa-sized packets.

    Args:
        data: Raw bytes to send (will have CRC32 appended)

    Returns:
        List of packets ready to transmit

    Raises:
        PackError: If data is too large (>16MB) or empty
    """
    if not data:
        raise PackError("Cannot pack empty data")

    # Append CRC32 for end-to-end verification
    payload = data + struct.pack(">I", crc32(data))
    total_len = len(payload)

    if total_len > 0xFFFFFFFF:
        raise PackError(f"Data too large: {total_len} bytes (max 4GB)")

    # Calculate packet count
    count = (total_len + MAX_PAYLOAD_PER_PACKET - 1) // MAX_PAYLOAD_PER_PACKET

    if count > 0xFFFF:
        raise PackError(f"Too many packets: {count} (max 65535)")

    packets = []
    for seq in range(count):
        start = seq * MAX_PAYLOAD_PER_PACKET
        end = min(start + MAX_PAYLOAD_PER_PACKET, total_len)
        chunk = payload[start:end]

        # Build header + payload
        header = struct.pack(HEADER_FMT, MAGIC, total_len, seq, count)
        pkt_data = header + chunk

        # Append per-packet CRC16
        pkt_crc = struct.pack(">H", crc16_ccitt(pkt_data))
        packets.append(pkt_data + pkt_crc)

    return packets


def unpack_packet(packet: bytes) -> StreamPacket:
    """
    Validate and parse a single packet.

    Args:
        packet: Raw packet bytes

    Returns:
        Parsed StreamPacket

    Raises:
        UnpackError: If packet is invalid or CRC fails
    """
    min_size = HEADER_SIZE + CRC16_SIZE
    if len(packet) < min_size:
        raise UnpackError(f"Packet too small: {len(packet)} < {min_size}")

    # Split into data and CRC
    pkt_data = packet[:-CRC16_SIZE]
    pkt_crc_bytes = packet[-CRC16_SIZE:]

    # Verify per-packet CRC16
    expected_crc = struct.unpack(">H", pkt_crc_bytes)[0]
    actual_crc = crc16_ccitt(pkt_data)
    if expected_crc != actual_crc:
        raise UnpackError(f"CRC16 mismatch: expected {expected_crc:04x}, got {actual_crc:04x}")

    # Parse header
    header = pkt_data[:HEADER_SIZE]
    magic, total_len, seq, count = struct.unpack(HEADER_FMT, header)

    if magic != MAGIC:
        raise UnpackError(f"Invalid magic: {magic:04x} != {MAGIC:04x}")

    payload = pkt_data[HEADER_SIZE:]

    return StreamPacket(total_len=total_len, seq=seq, count=count, payload=payload)


def unpack_stream(packets: list[bytes]) -> bytes:
    """
    Reassemble packets into original data.

    Args:
        packets: List of raw packet bytes (any order)

    Returns:
        Original data (without CRC32 suffix)

    Raises:
        UnpackError: If packets are invalid, incomplete, or CRC fails
    """
    if not packets:
        raise UnpackError("No packets to unpack")

    # Parse and validate all packets
    parsed: list[StreamPacket] = []
    for i, pkt in enumerate(packets):
        try:
            parsed.append(unpack_packet(pkt))
        except UnpackError as e:
            raise UnpackError(f"Packet {i}: {e}") from e

    # Verify consistency
    total_len = parsed[0].total_len
    count = parsed[0].count

    for p in parsed:
        if p.total_len != total_len:
            raise UnpackError(f"Inconsistent total_len: {p.total_len} != {total_len}")
        if p.count != count:
            raise UnpackError(f"Inconsistent count: {p.count} != {count}")

    # Check for duplicates and missing packets
    seqs = [p.seq for p in parsed]
    if len(seqs) != len(set(seqs)):
        raise UnpackError("Duplicate sequence numbers")

    missing = set(range(count)) - set(seqs)
    if missing:
        raise UnpackError(f"Missing packets: {sorted(missing)}")

    # Sort by sequence and reassemble
    parsed.sort(key=lambda p: p.seq)
    payload = b"".join(p.payload for p in parsed)

    if len(payload) != total_len:
        raise UnpackError(f"Reassembled size mismatch: {len(payload)} != {total_len}")

    # Verify final CRC32
    if len(payload) < CRC32_SIZE:
        raise UnpackError("Payload too small for CRC32")

    data = payload[:-CRC32_SIZE]
    crc_bytes = payload[-CRC32_SIZE:]
    expected_crc = struct.unpack(">I", crc_bytes)[0]
    actual_crc = crc32(data)

    if expected_crc != actual_crc:
        raise UnpackError(f"CRC32 mismatch: expected {expected_crc:08x}, got {actual_crc:08x}")

    return data


class PacketAssembler:
    """
    Stateful packet assembler for receiving streams.

    Buffers packets until a complete stream is received.
    Supports multiple concurrent streams (keyed by total_len + count).
    """

    def __init__(self, timeout: float = 30.0):
        """
        Args:
            timeout: Seconds before incomplete streams are discarded
        """
        self.timeout = timeout
        self._streams: dict[tuple[int, int], dict] = {}

    def add_packet(self, packet: bytes, current_time: float) -> bytes | None:
        """
        Add a packet to the assembler.

        Args:
            packet: Raw packet bytes
            current_time: Current timestamp (e.g., time.time())

        Returns:
            Complete reassembled data if stream is complete, else None

        Raises:
            UnpackError: If packet is invalid (CRC fail, bad header)
        """
        # Clean up old streams first
        self._cleanup(current_time)

        # Parse packet (raises UnpackError if invalid)
        parsed = unpack_packet(packet)

        # Stream key (total_len, count) - crude session ID
        key = (parsed.total_len, parsed.count)

        if key not in self._streams:
            self._streams[key] = {
                "packets": {},
                "first_seen": current_time,
            }

        stream = self._streams[key]
        stream["packets"][parsed.seq] = packet
        stream["last_seen"] = current_time

        # Check if complete
        if len(stream["packets"]) == parsed.count:
            # Extract packets in order
            packets = [stream["packets"][i] for i in range(parsed.count)]
            del self._streams[key]
            return unpack_stream(packets)

        return None

    def _cleanup(self, current_time: float) -> None:
        """Remove streams that have timed out."""
        expired = [
            key for key, stream in self._streams.items()
            if current_time - stream["first_seen"] > self.timeout
        ]
        for key in expired:
            del self._streams[key]

    def pending_streams(self) -> int:
        """Return number of incomplete streams being assembled."""
        return len(self._streams)

    def clear(self) -> None:
        """Clear all pending streams."""
        self._streams.clear()


# =============================================================================
# Optional FEC (Forward Error Correction) using XOR Parity
# =============================================================================

def xor_bytes(a: bytes, b: bytes) -> bytes:
    """
    XOR two byte sequences. Pads shorter sequence with zeros.

    Simple enough to implement in Arduino C:
        for (int i = 0; i < len; i++) result[i] = a[i] ^ b[i];
    """
    # Pad to same length
    max_len = max(len(a), len(b))
    a = a.ljust(max_len, b'\x00')
    b = b.ljust(max_len, b'\x00')
    return bytes(x ^ y for x, y in zip(a, b))


@dataclass
class ParityPacket:
    """A parity packet for FEC recovery."""
    total_len: int      # Same as data packets
    block_id: int       # Which block this parity covers (stored in seq field)
    block_size: int     # Number of data packets in block (stored in count field)
    first_seq: int      # First data packet seq in this block (derived)
    parity_data: bytes  # XOR of all data packet payloads in block


def _build_parity_packet(
    total_len: int,
    block_id: int,
    block_size: int,
    parity_data: bytes,
) -> bytes:
    """Build a parity packet with header and CRC."""
    # Parity packet: magic=0xDA7B, total_len, block_id (in seq), block_size (in count)
    header = struct.pack(HEADER_FMT, MAGIC_PARITY, total_len, block_id, block_size)
    pkt_data = header + parity_data
    pkt_crc = struct.pack(">H", crc16_ccitt(pkt_data))
    return pkt_data + pkt_crc


def _parse_parity_packet(packet: bytes) -> ParityPacket:
    """Parse a parity packet. Raises UnpackError if invalid."""
    min_size = HEADER_SIZE + CRC16_SIZE
    if len(packet) < min_size:
        raise UnpackError(f"Parity packet too small: {len(packet)} < {min_size}")

    pkt_data = packet[:-CRC16_SIZE]
    pkt_crc_bytes = packet[-CRC16_SIZE:]

    expected_crc = struct.unpack(">H", pkt_crc_bytes)[0]
    actual_crc = crc16_ccitt(pkt_data)
    if expected_crc != actual_crc:
        raise UnpackError(f"Parity CRC16 mismatch: expected {expected_crc:04x}, got {actual_crc:04x}")

    header = pkt_data[:HEADER_SIZE]
    magic, total_len, block_id, block_size = struct.unpack(HEADER_FMT, header)

    if magic != MAGIC_PARITY:
        raise UnpackError(f"Not a parity packet: magic {magic:04x}")

    parity_data = pkt_data[HEADER_SIZE:]

    return ParityPacket(
        total_len=total_len,
        block_id=block_id,
        block_size=block_size,
        first_seq=block_id * block_size,
        parity_data=parity_data,
    )


def pack_stream_with_fec(data: bytes, block_size: int = DEFAULT_FEC_BLOCK_SIZE) -> list[bytes]:
    """
    Pack data with XOR parity FEC.

    Adds one parity packet per block of data packets.
    Can recover from ONE lost packet per block.

    Args:
        data: Raw bytes to send
        block_size: Number of data packets per parity packet (default: 4)

    Returns:
        List of packets (data + parity) ready to transmit

    Example:
        block_size=4, 10 data packets -> packets 0-3, parity0, 4-7, parity1, 8-9, parity2
        Total: 10 data + 3 parity = 13 packets
    """
    if block_size < 1:
        raise PackError("block_size must be >= 1")

    # Get data packets using standard packing
    data_packets = pack_stream(data)
    total_len = len(data) + CRC32_SIZE  # Same as in data packet headers

    result = []
    num_blocks = (len(data_packets) + block_size - 1) // block_size

    for block_id in range(num_blocks):
        start_idx = block_id * block_size
        end_idx = min(start_idx + block_size, len(data_packets))
        block_packets = data_packets[start_idx:end_idx]

        # Add data packets for this block
        result.extend(block_packets)

        # Compute parity: XOR of all packet payloads (padded to same length)
        # We XOR the payload portion only, not headers
        parity = b'\x00' * MAX_PAYLOAD_PER_PACKET
        for pkt in block_packets:
            # Extract payload (between header and CRC16)
            payload = pkt[HEADER_SIZE:-CRC16_SIZE]
            parity = xor_bytes(parity, payload)

        # Build and add parity packet
        parity_pkt = _build_parity_packet(
            total_len=total_len,
            block_id=block_id,
            block_size=len(block_packets),  # Actual size (may be < block_size for last block)
            parity_data=parity,
        )
        result.append(parity_pkt)

    return result


def unpack_stream_with_fec(packets: list[bytes]) -> bytes:
    """
    Reassemble packets with FEC recovery.

    Can recover ONE lost data packet per block using parity.

    Args:
        packets: List of raw packets (data + parity, any order)

    Returns:
        Original data

    Raises:
        UnpackError: If unrecoverable (>1 lost per block) or invalid packets
    """
    if not packets:
        raise UnpackError("No packets to unpack")

    # Separate data and parity packets
    data_packets: dict[int, bytes] = {}  # seq -> packet
    parity_packets: dict[int, ParityPacket] = {}  # block_id -> parsed parity
    total_len = None
    total_count = None

    for pkt in packets:
        if len(pkt) < HEADER_SIZE:
            continue

        magic = struct.unpack(">H", pkt[:2])[0]

        if magic == MAGIC_DATA:
            try:
                parsed = unpack_packet(pkt)
                data_packets[parsed.seq] = pkt
                if total_len is None:
                    total_len = parsed.total_len
                    total_count = parsed.count
            except UnpackError:
                continue  # Skip invalid data packets

        elif magic == MAGIC_PARITY:
            try:
                parsed = _parse_parity_packet(pkt)
                parity_packets[parsed.block_id] = parsed
                if total_len is None:
                    total_len = parsed.total_len
            except UnpackError:
                continue  # Skip invalid parity packets

    if total_len is None or total_count is None:
        raise UnpackError("No valid packets found")

    # Try to recover missing packets using parity
    missing_seqs = set(range(total_count)) - set(data_packets.keys())

    for missing_seq in list(missing_seqs):
        # Find which block this seq belongs to
        # We need to figure out block_size from parity packets
        for block_id, parity in parity_packets.items():
            first_seq = parity.first_seq
            last_seq = first_seq + parity.block_size - 1

            if first_seq <= missing_seq <= last_seq:
                # This parity covers our missing packet
                block_seqs = list(range(first_seq, last_seq + 1))
                block_missing = [s for s in block_seqs if s not in data_packets]

                if len(block_missing) == 1:
                    # Can recover! XOR parity with all other packets in block
                    recovered_payload = parity.parity_data
                    for seq in block_seqs:
                        if seq in data_packets:
                            pkt = data_packets[seq]
                            payload = pkt[HEADER_SIZE:-CRC16_SIZE]
                            recovered_payload = xor_bytes(recovered_payload, payload)

                    # Rebuild the missing packet
                    header = struct.pack(HEADER_FMT, MAGIC_DATA, total_len, missing_seq, total_count)
                    pkt_data = header + recovered_payload
                    pkt_crc = struct.pack(">H", crc16_ccitt(pkt_data))
                    recovered_pkt = pkt_data + pkt_crc

                    data_packets[missing_seq] = recovered_pkt
                    missing_seqs.discard(missing_seq)
                    break

                elif len(block_missing) > 1:
                    # Can't recover more than one per block
                    raise UnpackError(
                        f"Block {block_id} missing {len(block_missing)} packets "
                        f"(seqs {block_missing}), can only recover 1"
                    )

    # Check if we have all packets now
    still_missing = set(range(total_count)) - set(data_packets.keys())
    if still_missing:
        raise UnpackError(f"Unrecoverable missing packets: {sorted(still_missing)}")

    # Reassemble using standard unpacking
    ordered_packets = [data_packets[i] for i in range(total_count)]
    return unpack_stream(ordered_packets)


# =============================================================================
# Optional Reed-Solomon FEC (requires 'reedsolo' package)
# =============================================================================

MAGIC_RS_PARITY = 0xDA7C  # RS parity packet magic

# RS parity packet header format:
# magic(2) + total_len(4) + parity_idx(2) + num_parity(2) + num_data(2) = 12 bytes
RS_HEADER_FMT = ">HIHHH"
RS_HEADER_SIZE = struct.calcsize(RS_HEADER_FMT)
RS_MAX_PAYLOAD = LORA_MAX_PACKET - RS_HEADER_SIZE - CRC16_SIZE  # 236 bytes


def _check_reedsolo() -> None:
    """Check if reedsolo is available, raise ImportError with helpful message if not."""
    try:
        import reedsolo  # noqa: F401
    except ImportError:
        raise ImportError(
            "Reed-Solomon FEC requires the 'reedsolo' package. "
            "Install with: pip install reedsolo"
        )


def pack_stream_with_rs_fec(
    data: bytes,
    num_parity: int = 2,
) -> list[bytes]:
    """
    Pack data with Reed-Solomon erasure coding.

    Creates K data packets + M parity packets. Can recover from ANY M lost packets
    (not just 1 per block like XOR parity).

    Args:
        data: Raw bytes to send
        num_parity: Number of parity packets to generate (default: 2)
                   Higher = more redundancy, more overhead

    Returns:
        List of packets (K data + M parity) ready to transmit

    Raises:
        PackError: If data too large or invalid parameters
        ImportError: If reedsolo package not installed

    Example:
        10 data packets + 2 parity = can lose ANY 2 packets and still recover
        Overhead: num_parity / num_data = 20% for 2 parity on 10 data

    Note:
        RS works on GF(2^8), so max K + M = 255 packets.
        For larger transfers, data is split into blocks.
    """
    _check_reedsolo()
    from reedsolo import RSCodec

    if num_parity < 1:
        raise PackError("num_parity must be >= 1")
    if num_parity > 32:
        raise PackError("num_parity too large (max 32 for practical use)")

    # Get data packets using standard packing
    data_packets = pack_stream(data)
    num_data = len(data_packets)

    if num_data + num_parity > 255:
        raise PackError(
            f"Too many packets for RS: {num_data} data + {num_parity} parity > 255. "
            "Split your data into smaller chunks."
        )

    total_len = len(data) + CRC32_SIZE

    # Pad all payloads to same length for RS encoding
    max_payload_len = max(len(pkt) - HEADER_SIZE - CRC16_SIZE for pkt in data_packets)

    # Extract payloads and pad
    payloads = []
    for pkt in data_packets:
        payload = pkt[HEADER_SIZE:-CRC16_SIZE]
        payload = payload.ljust(max_payload_len, b'\x00')
        payloads.append(payload)

    # Create RS codec for this configuration
    # RSCodec(nsym) creates a codec that adds nsym parity symbols
    rs = RSCodec(num_parity)

    # Apply RS encoding to each byte position across all packets
    # This creates num_parity parity bytes for each position
    parity_payloads = [bytearray(max_payload_len) for _ in range(num_parity)]

    for byte_pos in range(max_payload_len):
        # Gather this byte from all data packets
        column = bytes(payloads[i][byte_pos] for i in range(num_data))

        # Encode with RS - this appends num_parity parity bytes
        encoded = rs.encode(column)

        # Extract just the parity bytes (last num_parity bytes)
        parity_bytes = encoded[num_data:]

        # Distribute to parity payloads
        for i, b in enumerate(parity_bytes):
            parity_payloads[i][byte_pos] = b

    # Build parity packets
    parity_packets = []
    for parity_idx, parity_payload in enumerate(parity_payloads):
        header = struct.pack(
            RS_HEADER_FMT,
            MAGIC_RS_PARITY,
            total_len,
            parity_idx,
            num_parity,
            num_data,
        )
        pkt_data = header + bytes(parity_payload)
        pkt_crc = struct.pack(">H", crc16_ccitt(pkt_data))
        parity_packets.append(pkt_data + pkt_crc)

    return data_packets + parity_packets


@dataclass
class RSParityPacket:
    """A Reed-Solomon parity packet."""
    total_len: int
    parity_idx: int   # Which parity packet this is (0 to num_parity-1)
    num_parity: int   # Total number of parity packets
    num_data: int     # Number of data packets
    parity_data: bytes


def _parse_rs_parity_packet(packet: bytes) -> RSParityPacket:
    """Parse an RS parity packet. Raises UnpackError if invalid."""
    min_size = RS_HEADER_SIZE + CRC16_SIZE
    if len(packet) < min_size:
        raise UnpackError(f"RS parity packet too small: {len(packet)} < {min_size}")

    pkt_data = packet[:-CRC16_SIZE]
    pkt_crc_bytes = packet[-CRC16_SIZE:]

    expected_crc = struct.unpack(">H", pkt_crc_bytes)[0]
    actual_crc = crc16_ccitt(pkt_data)
    if expected_crc != actual_crc:
        raise UnpackError(f"RS parity CRC16 mismatch")

    header = pkt_data[:RS_HEADER_SIZE]
    magic, total_len, parity_idx, num_parity, num_data = struct.unpack(RS_HEADER_FMT, header)

    if magic != MAGIC_RS_PARITY:
        raise UnpackError(f"Not an RS parity packet: magic {magic:04x}")

    return RSParityPacket(
        total_len=total_len,
        parity_idx=parity_idx,
        num_parity=num_parity,
        num_data=num_data,
        parity_data=pkt_data[RS_HEADER_SIZE:],
    )


def unpack_stream_with_rs_fec(packets: list[bytes]) -> bytes:
    """
    Reassemble packets with Reed-Solomon erasure recovery.

    Can recover from ANY M lost packets (where M = num_parity).

    Args:
        packets: List of raw packets (data + RS parity, any order)

    Returns:
        Original data

    Raises:
        UnpackError: If too many packets lost to recover
        ImportError: If reedsolo package not installed
    """
    _check_reedsolo()
    from reedsolo import RSCodec, ReedSolomonError

    if not packets:
        raise UnpackError("No packets to unpack")

    # Separate data and RS parity packets
    data_packets: dict[int, bytes] = {}
    rs_parity_packets: dict[int, RSParityPacket] = {}
    total_len = None
    total_count = None
    num_parity = None
    num_data = None

    for pkt in packets:
        if len(pkt) < HEADER_SIZE:
            continue

        magic = struct.unpack(">H", pkt[:2])[0]

        if magic == MAGIC_DATA:
            try:
                parsed = unpack_packet(pkt)
                data_packets[parsed.seq] = pkt
                if total_len is None:
                    total_len = parsed.total_len
                    total_count = parsed.count
            except UnpackError:
                continue

        elif magic == MAGIC_RS_PARITY:
            try:
                parsed = _parse_rs_parity_packet(pkt)
                rs_parity_packets[parsed.parity_idx] = parsed
                if num_parity is None:
                    num_parity = parsed.num_parity
                    num_data = parsed.num_data
                    total_len = parsed.total_len
            except UnpackError:
                continue

    if total_len is None:
        raise UnpackError("No valid packets found")

    # If we have all data packets, no need for RS decoding
    if total_count and len(data_packets) == total_count:
        ordered = [data_packets[i] for i in range(total_count)]
        return unpack_stream(ordered)

    # Need RS recovery
    if num_parity is None or num_data is None:
        raise UnpackError("Missing RS parity information and not all data packets received")

    total_count = num_data
    missing_data_seqs = set(range(num_data)) - set(data_packets.keys())
    num_lost = len(missing_data_seqs)

    # Count available parity packets
    available_parity = len(rs_parity_packets)

    if num_lost > available_parity:
        raise UnpackError(
            f"Lost {num_lost} data packets but only have {available_parity} parity packets. "
            f"Need at least {num_lost} parity packets to recover."
        )

    # Determine payload length from received packets
    sample_pkt = next(iter(data_packets.values()), None)
    if sample_pkt:
        max_payload_len = len(sample_pkt) - HEADER_SIZE - CRC16_SIZE
    else:
        # Use parity packet payload length
        sample_parity = next(iter(rs_parity_packets.values()))
        max_payload_len = len(sample_parity.parity_data)

    # Pad all payloads to same length
    payloads: dict[int, bytes] = {}
    for seq, pkt in data_packets.items():
        payload = pkt[HEADER_SIZE:-CRC16_SIZE]
        payloads[seq] = payload.ljust(max_payload_len, b'\x00')

    # Create RS codec
    rs = RSCodec(num_parity)

    # Recover each byte position
    recovered_payloads: list[bytearray] = [bytearray(max_payload_len) for _ in range(num_data)]

    for byte_pos in range(max_payload_len):
        # Build the received codeword with erasures marked
        # RS decode expects: data_bytes + parity_bytes, with erasure positions
        received = bytearray(num_data + num_parity)
        erasure_pos = []

        # Fill in data bytes
        for seq in range(num_data):
            if seq in payloads:
                received[seq] = payloads[seq][byte_pos]
            else:
                received[seq] = 0  # Placeholder for erased
                erasure_pos.append(seq)

        # Fill in parity bytes
        for parity_idx in range(num_parity):
            if parity_idx in rs_parity_packets:
                parity = rs_parity_packets[parity_idx]
                if byte_pos < len(parity.parity_data):
                    received[num_data + parity_idx] = parity.parity_data[byte_pos]
                else:
                    received[num_data + parity_idx] = 0
                    erasure_pos.append(num_data + parity_idx)
            else:
                received[num_data + parity_idx] = 0
                erasure_pos.append(num_data + parity_idx)

        # Decode with erasure positions
        try:
            decoded = rs.decode(bytes(received), erase_pos=erasure_pos)
            # decoded is (data, parity, errata_pos) - we want just data
            if isinstance(decoded, tuple):
                data_bytes = decoded[0]
            else:
                data_bytes = decoded
        except ReedSolomonError as e:
            raise UnpackError(f"RS decode failed at byte {byte_pos}: {e}")

        # Store recovered data bytes
        for seq in range(num_data):
            recovered_payloads[seq][byte_pos] = data_bytes[seq]

    # Rebuild missing data packets
    for seq in missing_data_seqs:
        header = struct.pack(HEADER_FMT, MAGIC_DATA, total_len, seq, num_data)
        pkt_data = header + bytes(recovered_payloads[seq])
        pkt_crc = struct.pack(">H", crc16_ccitt(pkt_data))
        data_packets[seq] = pkt_data + pkt_crc

    # Now unpack normally
    ordered = [data_packets[i] for i in range(num_data)]
    return unpack_stream(ordered)
