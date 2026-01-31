import sys
from pathlib import Path
from unittest.mock import MagicMock

# Add project root to Python path for imports
sys.path.insert(0, str(Path(__file__).parent))

# Mock hardware-specific modules that aren't available on dev machines
sys.modules["gpiozero"] = MagicMock()
sys.modules["luma"] = MagicMock()
sys.modules["luma.core"] = MagicMock()
sys.modules["luma.core.interface"] = MagicMock()
sys.modules["luma.core.interface.serial"] = MagicMock()
sys.modules["luma.core.render"] = MagicMock()
sys.modules["luma.oled"] = MagicMock()
sys.modules["luma.oled.device"] = MagicMock()
