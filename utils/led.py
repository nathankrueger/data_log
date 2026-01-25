import threading
from time import sleep

from gpiozero import RGBLED


class RgbLed:
    def __init__(self, red_bcm: int, green_bcm: int, blue_bcm: int, common_anode: bool = True):
        self._led = RGBLED(red=red_bcm, green=green_bcm, blue=blue_bcm, active_high=not common_anode)
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


if __name__ == "__main__":
    dly = 1
    led = RgbLed(red_bcm=17, green_bcm=27, blue_bcm=22, common_anode=True)
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
