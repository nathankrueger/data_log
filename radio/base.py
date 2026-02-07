"""Abstract base class for radio communication."""

from abc import ABC, abstractmethod


class Radio(ABC):
    """
    Abstract base class for radio modules.

    Provides a common interface for different radio hardware (LoRa, etc.)
    allowing the node broadcaster and gateway to be radio-agnostic.
    """

    @abstractmethod
    def init(self) -> None:
        """
        Initialize the radio hardware.

        Should be called before any send/receive operations.
        May raise exceptions if hardware initialization fails.
        """
        pass

    @abstractmethod
    def send(self, data: bytes) -> bool:
        """
        Send data over the radio.

        Args:
            data: Bytes to transmit

        Returns:
            True if send completed, False on failure
        """
        pass

    @abstractmethod
    def receive(self, timeout: float = 5.0) -> bytes | None:
        """
        Receive data from the radio.

        Args:
            timeout: Maximum time to wait for data in seconds

        Returns:
            Received bytes, or None if timeout elapsed with no data
        """
        pass

    @abstractmethod
    def get_last_rssi(self) -> int | None:
        """
        Get the RSSI (signal strength) of the last received packet.

        Returns:
            RSSI in dBm, or None if not available
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """
        Clean up radio resources.

        Should be called when done with the radio to release hardware.
        """
        pass

    @abstractmethod
    def set_frequency(self, frequency_mhz: float) -> None:
        """
        Change the radio frequency at runtime.

        Args:
            frequency_mhz: New frequency in MHz (e.g., 915.0, 915.5)
        """
        pass

    def __enter__(self):
        """Context manager entry."""
        self.init()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False
