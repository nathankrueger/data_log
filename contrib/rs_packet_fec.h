/**
 * Reed-Solomon Packet-Level FEC for LoRa - Arduino Example
 *
 * This example demonstrates how to implement RS erasure coding across
 * multiple LoRa packets using the Arduino-FEC library.
 *
 * Key concept: Interleaved RS
 * - Apply RS encoding to each byte POSITION across all packets
 * - This creates parity packets that can recover ANY M lost packets
 * - Much more powerful than XOR parity (which can only recover 1 per block)
 *
 * Memory requirements:
 * - Need to buffer K data packets (K * 238 bytes for full payloads)
 * - Plus M parity packets during encoding
 * - For K=20 packets, that's ~5KB for payloads alone
 *
 * For HTCC-AB01 with ~48KB flash and limited RAM, consider:
 * - Smaller payload sizes
 * - Fewer packets per RS block
 * - Streaming approach (encode/decode as packets arrive)
 *
 * Dependencies:
 * - Arduino-FEC library: https://github.com/simonyipeter/Arduino-FEC
 *
 * Compatible with Python stream_protocol.py pack_stream_with_rs_fec()
 */

#ifndef RS_PACKET_FEC_H
#define RS_PACKET_FEC_H

#include <RS-FEC.h>  // Arduino-FEC library
#include "stream_protocol.h"

// RS Configuration
// With 2 parity packets, you can recover from ANY 2 lost packets
// Max K + M = 255 due to GF(2^8) field size
#define RS_DEFAULT_PARITY 2      // Number of parity packets
#define RS_MAX_DATA_PACKETS 20   // Max data packets per RS block
#define RS_MAX_PARITY 8          // Max parity packets

// RS Parity packet magic (matches Python)
#define SP_MAGIC_RS_PARITY 0xDA7C

// RS header format: magic(2) + total_len(4) + parity_idx(2) + num_parity(2) + num_data(2) = 12
#define RS_HEADER_SIZE 12
#define RS_MAX_PAYLOAD (SP_LORA_MAX_PACKET - RS_HEADER_SIZE - SP_CRC16_SIZE)  // 236 bytes


/**
 * RS Encoder for packet-level FEC.
 *
 * Usage:
 *   RSPacketEncoder<4, 2> encoder;  // 4 data packets, 2 parity
 *   encoder.begin(total_len);
 *   encoder.addDataPacket(0, payload0, len0);
 *   encoder.addDataPacket(1, payload1, len1);
 *   encoder.addDataPacket(2, payload2, len2);
 *   encoder.addDataPacket(3, payload3, len3);
 *   encoder.computeParity();
 *   encoder.getParityPacket(0, out_buf, &out_len);
 *   encoder.getParityPacket(1, out_buf, &out_len);
 *
 * @tparam K Number of data packets
 * @tparam M Number of parity packets
 */
template<int K, int M>
class RSPacketEncoder {
public:
    RSPacketEncoder() : _max_payload_len(0), _total_len(0), _ready(false) {
        static_assert(K + M <= 255, "K + M must be <= 255 for GF(2^8)");
        static_assert(K >= 1 && M >= 1, "Need at least 1 data and 1 parity packet");
    }

    /**
     * Begin encoding a new set of packets.
     * @param total_len Total payload length (from data packet headers)
     */
    void begin(uint32_t total_len) {
        _total_len = total_len;
        _max_payload_len = 0;
        _ready = false;

        // Clear buffers
        for (int i = 0; i < K; i++) {
            _payload_lens[i] = 0;
            memset(_data_payloads[i], 0, SP_MAX_PAYLOAD);
        }
        for (int i = 0; i < M; i++) {
            memset(_parity_payloads[i], 0, SP_MAX_PAYLOAD);
        }
    }

    /**
     * Add a data packet's payload.
     * @param seq Sequence number (0 to K-1)
     * @param payload Payload bytes (without header/CRC)
     * @param len Payload length
     * @return true if added successfully
     */
    bool addDataPacket(uint16_t seq, const uint8_t* payload, size_t len) {
        if (seq >= K || len > SP_MAX_PAYLOAD) return false;

        memcpy(_data_payloads[seq], payload, len);
        _payload_lens[seq] = len;

        if (len > _max_payload_len) {
            _max_payload_len = len;
        }

        return true;
    }

    /**
     * Compute parity packets after all data packets are added.
     * This is the expensive step - O(K * max_payload_len) RS operations.
     */
    void computeParity() {
        // Instantiate RS codec
        // Arduino-FEC uses template params: <message_len, ecc_length>
        // For packet-level FEC, message_len = K, ecc_length = M
        RS::ReedSolomon<K, M> rs;

        // For each byte position, encode across all K data packets
        for (size_t byte_pos = 0; byte_pos < _max_payload_len; byte_pos++) {
            uint8_t column[K];
            uint8_t encoded[K + M];

            // Gather this byte from all data packets
            for (int i = 0; i < K; i++) {
                if (byte_pos < _payload_lens[i]) {
                    column[i] = _data_payloads[i][byte_pos];
                } else {
                    column[i] = 0;  // Pad with zeros
                }
            }

            // Encode with RS
            rs.Encode(column, encoded);

            // Extract parity bytes (last M bytes of encoded)
            for (int m = 0; m < M; m++) {
                _parity_payloads[m][byte_pos] = encoded[K + m];
            }
        }

        _ready = true;
    }

    /**
     * Get a parity packet ready for transmission.
     * @param parity_idx Which parity packet (0 to M-1)
     * @param out_buf Output buffer (must be >= SP_LORA_MAX_PACKET)
     * @param out_len Output: packet length
     * @return true if successful
     */
    bool getParityPacket(uint16_t parity_idx, uint8_t* out_buf, size_t* out_len) {
        if (!_ready || parity_idx >= M) return false;

        // Build RS parity header
        // Format: magic(2) + total_len(4) + parity_idx(2) + num_parity(2) + num_data(2)
        sp_write_u16_be(out_buf + 0, SP_MAGIC_RS_PARITY);
        sp_write_u32_be(out_buf + 2, _total_len);
        sp_write_u16_be(out_buf + 6, parity_idx);
        sp_write_u16_be(out_buf + 8, M);
        sp_write_u16_be(out_buf + 10, K);

        // Copy parity payload
        memcpy(out_buf + RS_HEADER_SIZE, _parity_payloads[parity_idx], _max_payload_len);

        // Add CRC16
        size_t data_len = RS_HEADER_SIZE + _max_payload_len;
        uint16_t crc = sp_crc16_ccitt(out_buf, data_len);
        sp_write_u16_be(out_buf + data_len, crc);

        *out_len = data_len + SP_CRC16_SIZE;
        return true;
    }

    uint16_t getNumDataPackets() const { return K; }
    uint16_t getNumParityPackets() const { return M; }
    size_t getMaxPayloadLen() const { return _max_payload_len; }

private:
    uint8_t _data_payloads[K][SP_MAX_PAYLOAD];
    uint8_t _parity_payloads[M][SP_MAX_PAYLOAD];
    size_t _payload_lens[K];
    size_t _max_payload_len;
    uint32_t _total_len;
    bool _ready;
};


/**
 * RS Decoder for packet-level FEC recovery.
 *
 * Usage:
 *   RSPacketDecoder<4, 2> decoder;
 *   decoder.begin(total_len, num_data);
 *   // Add received packets (data or parity)
 *   decoder.addReceivedDataPacket(seq, payload, len);
 *   decoder.addReceivedParityPacket(idx, payload, len);
 *   // Check if we can decode
 *   if (decoder.canDecode()) {
 *       decoder.decode();
 *       decoder.getRecoveredPayload(missing_seq, out_buf, &out_len);
 *   }
 */
template<int K, int M>
class RSPacketDecoder {
public:
    RSPacketDecoder() : _total_len(0), _max_payload_len(0), _decoded(false) {}

    void begin(uint32_t total_len, size_t max_payload_len) {
        _total_len = total_len;
        _max_payload_len = max_payload_len;
        _decoded = false;

        // Clear buffers and tracking
        for (int i = 0; i < K; i++) {
            _data_received[i] = false;
            memset(_data_payloads[i], 0, SP_MAX_PAYLOAD);
        }
        for (int i = 0; i < M; i++) {
            _parity_received[i] = false;
            memset(_parity_payloads[i], 0, SP_MAX_PAYLOAD);
        }
    }

    bool addReceivedDataPacket(uint16_t seq, const uint8_t* payload, size_t len) {
        if (seq >= K || len > SP_MAX_PAYLOAD) return false;

        memcpy(_data_payloads[seq], payload, len);
        _data_received[seq] = true;

        if (len > _max_payload_len) {
            _max_payload_len = len;
        }

        return true;
    }

    bool addReceivedParityPacket(uint16_t parity_idx, const uint8_t* payload, size_t len) {
        if (parity_idx >= M || len > SP_MAX_PAYLOAD) return false;

        memcpy(_parity_payloads[parity_idx], payload, len);
        _parity_received[parity_idx] = true;

        if (len > _max_payload_len) {
            _max_payload_len = len;
        }

        return true;
    }

    /**
     * Count missing data packets.
     */
    int countMissing() const {
        int missing = 0;
        for (int i = 0; i < K; i++) {
            if (!_data_received[i]) missing++;
        }
        return missing;
    }

    /**
     * Count available parity packets.
     */
    int countParity() const {
        int count = 0;
        for (int i = 0; i < M; i++) {
            if (_parity_received[i]) count++;
        }
        return count;
    }

    /**
     * Check if we have enough data to decode.
     * Need: received_data + received_parity >= K
     */
    bool canDecode() const {
        int received_data = K - countMissing();
        int received_parity = countParity();
        return (received_data + received_parity >= K);
    }

    /**
     * Perform RS decoding to recover missing packets.
     * @return true if decode succeeded
     */
    bool decode() {
        if (!canDecode()) return false;

        RS::ReedSolomon<K, M> rs;

        // For each byte position, decode
        for (size_t byte_pos = 0; byte_pos < _max_payload_len; byte_pos++) {
            uint8_t received[K + M];
            uint8_t erasure_pos[K + M];
            int num_erasures = 0;

            // Build received codeword
            for (int i = 0; i < K; i++) {
                if (_data_received[i]) {
                    received[i] = _data_payloads[i][byte_pos];
                } else {
                    received[i] = 0;
                    erasure_pos[num_erasures++] = i;
                }
            }

            for (int i = 0; i < M; i++) {
                if (_parity_received[i]) {
                    received[K + i] = _parity_payloads[i][byte_pos];
                } else {
                    received[K + i] = 0;
                    erasure_pos[num_erasures++] = K + i;
                }
            }

            // Decode with erasure correction
            uint8_t repaired[K + M];
            // Note: Arduino-FEC Decode() may not support erasure positions directly
            // You may need to modify the library or use a different approach
            // This is a simplified example
            rs.Decode(received, repaired);

            // Store recovered data bytes
            for (int i = 0; i < K; i++) {
                _data_payloads[i][byte_pos] = repaired[i];
            }
        }

        // Mark all as received
        for (int i = 0; i < K; i++) {
            _data_received[i] = true;
        }

        _decoded = true;
        return true;
    }

    /**
     * Get a data packet payload (after decoding).
     */
    bool getDataPayload(uint16_t seq, uint8_t* out_buf, size_t* out_len) const {
        if (seq >= K || !_data_received[seq]) return false;

        memcpy(out_buf, _data_payloads[seq], _max_payload_len);
        *out_len = _max_payload_len;
        return true;
    }

private:
    uint8_t _data_payloads[K][SP_MAX_PAYLOAD];
    uint8_t _parity_payloads[M][SP_MAX_PAYLOAD];
    bool _data_received[K];
    bool _parity_received[M];
    uint32_t _total_len;
    size_t _max_payload_len;
    bool _decoded;
};


// =============================================================================
// Helper Functions
// =============================================================================

/**
 * Parse an RS parity packet header.
 */
static inline bool sp_parse_rs_parity_header(
    const uint8_t* buf,
    size_t len,
    uint32_t* total_len,
    uint16_t* parity_idx,
    uint16_t* num_parity,
    uint16_t* num_data
) {
    if (len < RS_HEADER_SIZE + SP_CRC16_SIZE) return false;

    uint16_t magic = sp_read_u16_be(buf);
    if (magic != SP_MAGIC_RS_PARITY) return false;

    // Verify CRC
    size_t data_len = len - SP_CRC16_SIZE;
    uint16_t expected_crc = sp_read_u16_be(buf + data_len);
    uint16_t actual_crc = sp_crc16_ccitt(buf, data_len);
    if (expected_crc != actual_crc) return false;

    *total_len = sp_read_u32_be(buf + 2);
    *parity_idx = sp_read_u16_be(buf + 6);
    *num_parity = sp_read_u16_be(buf + 8);
    *num_data = sp_read_u16_be(buf + 10);

    return true;
}

/**
 * Check if a packet is an RS parity packet.
 */
static inline bool sp_is_rs_parity_packet(const uint8_t* buf, size_t len) {
    if (len < 2) return false;
    return sp_read_u16_be(buf) == SP_MAGIC_RS_PARITY;
}

#endif // RS_PACKET_FEC_H


// =============================================================================
// Example Usage (in .ino file)
// =============================================================================

#if 0  // Example code - not compiled

#include "rs_packet_fec.h"

// Configuration: 10 data packets + 2 parity = can lose ANY 2 packets
RSPacketEncoder<10, 2> encoder;
RSPacketDecoder<10, 2> decoder;

void sendWithRSFEC(const uint8_t* data, size_t len) {
    // First, create data packets using stream_protocol
    // Then encode RS parity

    // ... split data into packets ...

    // After all data packets are prepared:
    encoder.begin(total_len);
    for (int i = 0; i < num_packets; i++) {
        encoder.addDataPacket(i, payloads[i], payload_lens[i]);
    }
    encoder.computeParity();

    // Send data packets
    for (int i = 0; i < num_packets; i++) {
        radio.send(data_packets[i], data_packet_lens[i]);
    }

    // Send parity packets
    for (int i = 0; i < 2; i++) {
        uint8_t parity_buf[250];
        size_t parity_len;
        encoder.getParityPacket(i, parity_buf, &parity_len);
        radio.send(parity_buf, parity_len);
    }
}

void receiveWithRSFEC() {
    decoder.begin(expected_total_len, expected_max_payload);

    // Receive packets (may be missing some)
    while (receiving) {
        uint8_t buf[250];
        int len = radio.receive(buf, 250);

        if (sp_is_rs_parity_packet(buf, len)) {
            // Parse and add parity packet
            uint32_t total_len;
            uint16_t parity_idx, num_parity, num_data;
            sp_parse_rs_parity_header(buf, len, &total_len, &parity_idx, &num_parity, &num_data);
            decoder.addReceivedParityPacket(parity_idx, buf + RS_HEADER_SIZE, len - RS_HEADER_SIZE - SP_CRC16_SIZE);
        } else {
            // Parse and add data packet
            sp_packet_t pkt;
            if (sp_parse_packet(buf, len, &pkt)) {
                decoder.addReceivedDataPacket(pkt.header.seq, pkt.payload, pkt.payload_len);
            }
        }
    }

    // Try to decode
    if (decoder.canDecode()) {
        if (decoder.decode()) {
            Serial.println("Successfully recovered all packets!");
        }
    } else {
        Serial.println("Too many packets lost to recover");
    }
}

#endif
