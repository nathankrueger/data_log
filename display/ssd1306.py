"""SSD1306 OLED display implementation."""

from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306

from .base import Display


class SSD1306Display(Display):
    """SSD1306 OLED display implementation using luma.oled."""

    def __init__(self, i2c_port: int = 1, i2c_address: int = 0x3C):
        serial = i2c(port=i2c_port, address=i2c_address)
        self._device = ssd1306(serial)

    @property
    def width(self) -> int:
        return 128

    @property
    def height(self) -> int:
        return 64

    @property
    def line_height(self) -> int:
        return 16

    def show(self) -> None:
        self._device.show()

    def hide(self) -> None:
        self._device.hide()

    def clear(self) -> None:
        self._device.clear()

    def render_lines(self, lines: list[str | None]) -> None:
        with canvas(self._device) as draw:
            y = 0
            for line in lines[: self.max_lines]:
                if line is not None:
                    draw.text((0, y), line, fill="white")
                y += self.line_height
