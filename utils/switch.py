from gpiozero import Button
from signal import pause
from time import sleep

# Define the microswitch connected to GPIO 16
# Button defaults to using an internal pull-up resistor (3.3V)
# We wired the switch to ground, so it pulls down when pressed.
microswitch = Button(16)

def switch_pressed():
    print("Microswitch Activated! (Door Closed/Limit Reached)")

def switch_released():
    print("Microswitch Released!")

# Set up the event handlers
microswitch.when_pressed = switch_pressed
microswitch.when_released = switch_released

print("Waiting for microswitch input... Press Ctrl+C to exit")

while True:
    print("Main Loop...")
    sleep(1)
