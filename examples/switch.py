from gpiozero import Button
from signal import pause
from time import sleep
import threading

# Define the microswitch connected to GPIO 16
# Button defaults to using an internal pull-up resistor (3.3V)
# We wired the switch to ground, so it pulls down when pressed.
microswitch = Button(16)

def switch_pressed():
    thread = threading.current_thread()
    print(f"--- Switch Pressed! --- [thread_id: {thread.ident}]")

def switch_released():
    thread = threading.current_thread()
    print(f"--- Switch Released! --- [thread_id: {thread.ident}]")

# Set up the event handlers
microswitch.when_pressed = switch_pressed
microswitch.when_released = switch_released

print("Waiting for microswitch input... Press Ctrl+C to exit")

while True:
    thread = threading.current_thread()
    print(f"Main Loop...  [thread_id: {thread.ident}]")
    sleep(1)
