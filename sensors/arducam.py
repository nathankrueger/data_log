import time
from pathlib import Path
from picamera2 import Picamera2

### Install binaries, installing picamera2 with pip takes too long as it needs to compile code
# sudo apt update
# sudo apt install python3-picamera2 python3-opencv python3-numpy -y
#
### Use this to see if camera is there:
# sudo apt install libcamera-apps -y
# rpicam-hello --list-cameras
#
### Update firmware config
# sudo vim /boot/firmware/config.txt
#   Add the following lines:
#       camera_auto_detect=0
#       dtoverlay=imx477

# Initialize the camera
picam2 = Picamera2()
#config = picam2.create_still_configuration(main_size=(4056, 3040))
config = picam2.create_still_configuration(main={"size": (2028, 1520)})
picam2.configure(config)
picam2.start()

try:
    print("Waiting for auto-exposure to settle...")
    time.sleep(2)  # Essential for the HQ camera to calibrate light levels

    # Define file path
    output_path = Path(__file__).parent / "timed_capture.jpg"

    print("Capturing image...")
    
    # Start the timer
    start_time = time.time()

    # Capture the high-res file
    picam2.capture_file(output_path)

    # Calculate elapsed time
    end_time = time.time()
    duration = end_time - start_time

    print("-" * 30)
    print(f"Capture Successful!")
    print(f"File saved to: {output_path}")
    print(f"Time taken to capture and encode: {duration:.2f} seconds")
    print("-" * 30)

finally:
    picam2.stop()