import threading
from time import sleep


class RgbLed:
    def __init__(self, red_bcm: int, green_bcm: int, blue_bcm: int, common_anode: bool = True):
        from gpiozero import RGBLED
        self._led = RGBLED(
            red=red_bcm,
            green=green_bcm,
            blue=blue_bcm,
            active_high=not common_anode,
            initial_value=(0, 0, 0),
        )
        self._base_color: tuple[int, int, int] = (0, 0, 0)
        self._flash_gen = 0
        self._flash_lock = threading.Lock()

    def set_rgb(self, r: int, g: int, b: int) -> None:
        """Sets the LED color using 0-255 values for R, G, and B."""
        self._led.color = (r / 255.0, g / 255.0, b / 255.0)

    def set_base_color(self, r: int, g: int, b: int) -> None:
        """Sets the base color that flash() returns to."""
        self._base_color = (r, g, b)
        self.set_rgb(r, g, b)

    def flash(self, r: int, g: int, b: int, duration: float) -> None:
        """Flash a color for duration seconds, then restore base color. Non-blocking.

        If a new flash is requested while one is in progress, the newer flash
        takes priority and the older flash's restoration is cancelled.
        """
        with self._flash_lock:
            self._flash_gen += 1
            my_gen = self._flash_gen

        def _flash():
            self.set_rgb(r, g, b)
            sleep(duration)
            with self._flash_lock:
                # Only restore if no newer flash has started
                if self._flash_gen == my_gen:
                    self.set_rgb(*self._base_color)

        thread = threading.Thread(target=_flash, daemon=True)
        thread.start()

    def off(self) -> None:
        """Turn off the LED."""
        self._led.off()

    def close(self) -> None:
        """Clean up GPIO resources."""
        self._led.close()


# Color name to RGB tuple mapping (matches AB01)
COLOR_MAP: dict[str, tuple[int, int, int]] = {
    # Full names
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "white": (255, 255, 255),
    "off": (0, 0, 0),
    # Single-letter shortcuts
    "r": (255, 0, 0),
    "g": (0, 255, 0),
    "b": (0, 0, 255),
    "y": (255, 255, 0),
    "c": (0, 255, 255),
    "m": (255, 0, 255),
    "w": (255, 255, 255),
    "o": (0, 0, 0),
}


def parse_color(color_str: str) -> tuple[int, int, int] | None:
    """
    Parse a color name string to RGB tuple.

    Accepts full names (e.g., "red") or single-letter shortcuts (e.g., "r").
    Returns None for unrecognized colors.
    """
    return COLOR_MAP.get(color_str.lower())


def scale_brightness(rgb: tuple[int, int, int], brightness: int) -> tuple[int, int, int]:
    """
    Scale an RGB tuple by brightness (0-255).

    Args:
        rgb: Base RGB tuple with values 0-255
        brightness: Brightness scalar 0-255

    Returns:
        Scaled RGB tuple
    """
    scale = brightness / 255.0
    return (
        int(rgb[0] * scale),
        int(rgb[1] * scale),
        int(rgb[2] * scale),
    )


if __name__ == "__main__":
    dly = 1
    led = RgbLed(red_bcm=17, green_bcm=22, blue_bcm=27, common_anode=True)
    try:
        while True:
            print("RED")
            led.set_rgb(255, 0, 0)
            sleep(dly)

            print("GREEN")
            led.set_rgb(0, 255, 0)
            sleep(1)

            print("BLUE")
            led.set_rgb(0, 0, 255)
            sleep(1)

            print("PURPLE")
            led.set_rgb(15, 0, 255)
            sleep(1)

    except KeyboardInterrupt:
        led.close()
