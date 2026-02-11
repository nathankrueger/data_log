"""Tests for RadioState class."""

import threading
import pytest
from unittest.mock import MagicMock

from utils.radio_state import RadioState, BW_HZ_MAP, BW_CODE_MAP


@pytest.fixture
def mock_radio():
    """Create a mock radio with settable properties."""
    radio = MagicMock()
    radio.spreading_factor = 7
    radio.signal_bandwidth = 125000
    radio.tx_power = 23
    return radio


@pytest.fixture
def radio_state(mock_radio):
    """Create a RadioState with mock radio."""
    return RadioState(
        radio=mock_radio,
        n2g_freq=915.0,
        g2n_freq=915.5,
    )


class TestRadioStateBasics:
    """Test basic RadioState properties."""

    def test_radio_property(self, radio_state, mock_radio):
        """Radio property returns the hardware instance."""
        assert radio_state.radio is mock_radio

    def test_initial_frequencies(self, radio_state):
        """Frequencies are set from constructor."""
        assert radio_state.n2g_freq == 915.0
        assert radio_state.g2n_freq == 915.5

    def test_frequency_setters(self, radio_state):
        """Frequencies can be changed."""
        radio_state.n2g_freq = 916.0
        radio_state.g2n_freq = 916.5
        assert radio_state.n2g_freq == 916.0
        assert radio_state.g2n_freq == 916.5

    def test_hardware_properties(self, radio_state, mock_radio):
        """Hardware properties delegate to radio."""
        assert radio_state.spreading_factor == 7
        assert radio_state.signal_bandwidth == 125000
        assert radio_state.tx_power == 23

    def test_bandwidth_code(self, radio_state, mock_radio):
        """Bandwidth code converts from Hz."""
        mock_radio.signal_bandwidth = 125000
        assert radio_state.bandwidth_code == 0

        mock_radio.signal_bandwidth = 250000
        assert radio_state.bandwidth_code == 1

        mock_radio.signal_bandwidth = 500000
        assert radio_state.bandwidth_code == 2


class TestPendingStorage:
    """Test pending/staged value storage."""

    def test_set_and_get_pending(self, radio_state):
        """Can set and get pending values."""
        radio_state.set_pending("sf", "10")
        assert radio_state.get_pending("sf") == "10"

    def test_get_pending_none(self, radio_state):
        """Get pending returns None for unset values."""
        assert radio_state.get_pending("sf") is None

    def test_clear_pending(self, radio_state):
        """Clear pending removes the value."""
        radio_state.set_pending("sf", "10")
        radio_state.clear_pending("sf")
        assert radio_state.get_pending("sf") is None

    def test_get_all_pending(self, radio_state):
        """Get all pending returns copy of dict."""
        radio_state.set_pending("sf", "10")
        radio_state.set_pending("bw", "1")

        pending = radio_state.get_all_pending()
        assert pending == {"sf": "10", "bw": "1"}

        # Verify it's a copy
        pending["txpwr"] = "20"
        assert radio_state.get_pending("txpwr") is None

    def test_clear_all_pending(self, radio_state):
        """Clear all pending removes all values."""
        radio_state.set_pending("sf", "10")
        radio_state.set_pending("bw", "1")
        radio_state.clear_all_pending()

        assert radio_state.get_pending("sf") is None
        assert radio_state.get_pending("bw") is None

    def test_has_pending(self, radio_state):
        """Has pending checks if any values are staged."""
        assert radio_state.has_pending() is False

        radio_state.set_pending("sf", "10")
        assert radio_state.has_pending() is True

        radio_state.clear_all_pending()
        assert radio_state.has_pending() is False


class TestApplyPending:
    """Test applying pending values to hardware."""

    def test_apply_pending_empty(self, radio_state):
        """Apply pending with no values returns empty list."""
        result = radio_state.apply_pending()
        assert result == []

    def test_apply_pending_sf(self, radio_state, mock_radio):
        """Apply pending SF changes hardware."""
        radio_state.set_pending("sf", "10")
        result = radio_state.apply_pending()

        assert "sf=10" in result
        assert mock_radio.spreading_factor == 10
        assert radio_state.get_pending("sf") is None  # Cleared

    def test_apply_pending_bw(self, radio_state, mock_radio):
        """Apply pending BW converts code to Hz."""
        radio_state.set_pending("bw", "2")  # 500kHz
        result = radio_state.apply_pending()

        assert "bw=2" in result
        assert mock_radio.signal_bandwidth == 500000
        assert radio_state.get_pending("bw") is None

    def test_apply_pending_txpwr(self, radio_state, mock_radio):
        """Apply pending TX power changes hardware."""
        radio_state.set_pending("txpwr", "15")
        result = radio_state.apply_pending()

        assert "txpwr=15" in result
        assert mock_radio.tx_power == 15

    def test_apply_pending_n2gfreq(self, radio_state):
        """Apply pending N2G freq converts Hz to MHz."""
        radio_state.set_pending("n2gfreq", "916000000")
        result = radio_state.apply_pending()

        assert "n2gfreq=916000000" in result
        assert radio_state.n2g_freq == 916.0

    def test_apply_pending_g2nfreq(self, radio_state):
        """Apply pending G2N freq converts Hz to MHz."""
        radio_state.set_pending("g2nfreq", "916500000")
        result = radio_state.apply_pending()

        assert "g2nfreq=916500000" in result
        assert radio_state.g2n_freq == 916.5

    def test_apply_pending_multiple(self, radio_state, mock_radio):
        """Apply pending handles multiple values."""
        radio_state.set_pending("sf", "9")
        radio_state.set_pending("bw", "1")
        radio_state.set_pending("txpwr", "20")

        result = radio_state.apply_pending()

        assert len(result) == 3
        assert mock_radio.spreading_factor == 9
        assert mock_radio.signal_bandwidth == 250000
        assert mock_radio.tx_power == 20
        assert not radio_state.has_pending()


class TestEffectiveValues:
    """Test get_effective_* methods that return pending if staged."""

    def test_effective_sf_no_pending(self, radio_state, mock_radio):
        """Effective SF returns hardware value when no pending."""
        mock_radio.spreading_factor = 8
        assert radio_state.get_effective_sf() == 8

    def test_effective_sf_with_pending(self, radio_state, mock_radio):
        """Effective SF returns pending value when staged."""
        mock_radio.spreading_factor = 8
        radio_state.set_pending("sf", "10")
        assert radio_state.get_effective_sf() == 10

    def test_effective_bw_no_pending(self, radio_state, mock_radio):
        """Effective BW returns hardware code when no pending."""
        mock_radio.signal_bandwidth = 250000  # Code 1
        assert radio_state.get_effective_bw() == 1

    def test_effective_bw_with_pending(self, radio_state):
        """Effective BW returns pending code when staged."""
        radio_state.set_pending("bw", "2")
        assert radio_state.get_effective_bw() == 2

    def test_effective_txpwr_no_pending(self, radio_state, mock_radio):
        """Effective TX power returns hardware value when no pending."""
        mock_radio.tx_power = 20
        assert radio_state.get_effective_txpwr() == 20

    def test_effective_txpwr_with_pending(self, radio_state):
        """Effective TX power returns pending value when staged."""
        radio_state.set_pending("txpwr", "15")
        assert radio_state.get_effective_txpwr() == 15

    def test_effective_n2g_freq_no_pending(self, radio_state):
        """Effective N2G freq returns current MHz as Hz when no pending."""
        radio_state.n2g_freq = 916.0
        assert radio_state.get_effective_n2g_freq_hz() == 916000000

    def test_effective_n2g_freq_with_pending(self, radio_state):
        """Effective N2G freq returns pending Hz when staged."""
        radio_state.set_pending("n2gfreq", "917000000")
        assert radio_state.get_effective_n2g_freq_hz() == 917000000

    def test_effective_g2n_freq_no_pending(self, radio_state):
        """Effective G2N freq returns current MHz as Hz when no pending."""
        radio_state.g2n_freq = 916.5
        assert radio_state.get_effective_g2n_freq_hz() == 916500000

    def test_effective_g2n_freq_with_pending(self, radio_state):
        """Effective G2N freq returns pending Hz when staged."""
        radio_state.set_pending("g2nfreq", "917500000")
        assert radio_state.get_effective_g2n_freq_hz() == 917500000


class TestThreadSafety:
    """Test thread-safety of RadioState."""

    def test_concurrent_pending_access(self, radio_state):
        """Concurrent set/get pending doesn't corrupt state."""
        errors = []

        def writer():
            for i in range(100):
                radio_state.set_pending("sf", str(i % 6 + 7))

        def reader():
            for _ in range(100):
                val = radio_state.get_pending("sf")
                if val is not None:
                    try:
                        sf = int(val)
                        if sf < 7 or sf > 12:
                            errors.append(f"Invalid SF: {sf}")
                    except ValueError:
                        errors.append(f"Non-integer SF: {val}")

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=writer),
            threading.Thread(target=reader),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []

    def test_concurrent_frequency_access(self, radio_state):
        """Concurrent frequency read/write doesn't corrupt state."""
        errors = []

        def writer():
            for i in range(100):
                radio_state.n2g_freq = 915.0 + (i % 10) * 0.1

        def reader():
            for _ in range(100):
                freq = radio_state.n2g_freq
                if freq < 915.0 or freq > 916.0:
                    errors.append(f"Invalid freq: {freq}")

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=writer),
            threading.Thread(target=reader),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


class TestBandwidthMaps:
    """Test bandwidth code/Hz mappings."""

    def test_bw_hz_map_values(self):
        """BW_HZ_MAP has correct values."""
        assert BW_HZ_MAP[0] == 125000
        assert BW_HZ_MAP[1] == 250000
        assert BW_HZ_MAP[2] == 500000

    def test_bw_code_map_inverse(self):
        """BW_CODE_MAP is inverse of BW_HZ_MAP."""
        assert BW_CODE_MAP[125000] == 0
        assert BW_CODE_MAP[250000] == 1
        assert BW_CODE_MAP[500000] == 2
