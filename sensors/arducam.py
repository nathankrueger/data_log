"""
Arducam IMX477 camera capture and 7-segment OCR.

Provides camera capture functionality and OCR for 7-segment displays.
Can be used as a library (capture_and_ocr function) or CLI tool.

Setup Notes:
    # Install binaries (faster than pip)
    sudo apt update
    sudo apt install python3-picamera2 python3-opencv python3-numpy python3-pil -y

    # Check if camera is detected
    sudo apt install libcamera-apps -y
    rpicam-hello --list-cameras

    # Install ssocr for 7-segment display OCR
    sudo apt install ssocr -y

    # Update firmware config (/boot/firmware/config.txt)
    camera_auto_detect=0
    dtoverlay=imx477
"""

import argparse
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# Available sizes from rpicam-hello --list-cameras (imx477)
AVAILABLE_SIZES = {
    "1332x990": (1332, 990),  # 120.05 fps
    "2028x1080": (2028, 1080),  # 50.03 fps
    "2028x1520": (2028, 1520),  # 40.01 fps
    "4056x3040": (4056, 3040),  # 10.00 fps (full resolution)
}
DEFAULT_SIZE = "4056x3040"


def detect_display(image_path, min_area=5000, max_area_ratio=0.05):
    """
    Auto-detect 7-segment LCD/LED displays in an image using contour detection.

    Args:
        image_path: Path to the image file
        min_area: Minimum contour area to consider
        max_area_ratio: Maximum ratio of image area for a display region

    Returns:
        List of bounding boxes (x, y, w, h) sorted by score (best first).
    """
    img = cv2.imread(str(image_path))
    if img is None:
        print(f"Error: Could not load image {image_path}")
        return []

    height, width = img.shape[:2]
    max_area = width * height * max_area_ratio
    img_center_x, img_center_y = width // 2, height // 2

    # Convert to grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Apply bilateral filter to reduce noise while keeping edges sharp
    filtered = cv2.bilateralFilter(gray, 9, 75, 75)

    # Adaptive threshold to handle varying lighting
    thresh = cv2.adaptiveThreshold(
        filtered,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        11,
        2,
    )

    # Find contours
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area or area > max_area:
            continue

        # Get bounding rectangle
        x, y, w, h = cv2.boundingRect(contour)

        # Filter by aspect ratio (displays are typically wider than tall, or square-ish)
        aspect = w / h if h > 0 else 0
        if aspect < 0.3 or aspect > 3:
            continue

        # Check for rectangular shape (displays are usually rectangular)
        rect = cv2.minAreaRect(contour)
        box = cv2.boxPoints(rect)
        box_area = cv2.contourArea(box)
        rectangularity = area / box_area if box_area > 0 else 0
        if rectangularity < 0.6:
            continue

        # Check contrast in the region (displays have high contrast)
        roi = gray[y : y + h, x : x + w]
        if roi.size > 0:
            std_dev = np.std(roi)
            if std_dev < 30:  # Low contrast, probably not a display
                continue

        # Calculate distance from center (prefer centered regions)
        center_x, center_y = x + w // 2, y + h // 2
        dist_from_center = np.sqrt(
            (center_x - img_center_x) ** 2 + (center_y - img_center_y) ** 2
        )
        max_dist = np.sqrt(img_center_x**2 + img_center_y**2)
        center_score = 1 - (dist_from_center / max_dist)  # 0-1, higher = more centered

        # LCD displays tend to be dark with lighter digits - check average brightness
        avg_brightness = np.mean(roi)
        darkness_score = 1 - (avg_brightness / 255)  # Prefer darker regions (LCD panels)

        # Combined score: prefer centered, darker, moderately-sized rectangles
        score = center_score * 0.5 + darkness_score * 0.3 + rectangularity * 0.2

        candidates.append((x, y, w, h, score))

    # Sort by score (highest first) and return without the score
    candidates.sort(key=lambda c: c[4], reverse=True)
    return [(x, y, w, h) for x, y, w, h, _ in candidates]


def run_ocr(image_path, crop_region):
    """
    Run ssocr on a cropped region of the image.

    Args:
        image_path: Path to the image file
        crop_region: Tuple of (x, y, w, h) for the crop region

    Returns:
        OCR result string, or None if recognition failed.
    """
    x, y, w, h = crop_region

    # Load and crop using OpenCV for preprocessing
    img = cv2.imread(str(image_path))
    cropped = img[y : y + h, x : x + w]

    # Convert to grayscale
    gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)

    # Resize for better OCR (ssocr works better with larger images)
    scale = 3
    gray = cv2.resize(
        gray,
        (gray.shape[1] * scale, gray.shape[0] * scale),
        interpolation=cv2.INTER_CUBIC,
    )

    # Save grayscale cropped image
    gray_path = Path(image_path).parent / "ocr_gray.png"
    cv2.imwrite(str(gray_path), gray)

    # Normalize to use full 0-255 range (stretch contrast)
    normalized = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)

    # Light blur to reduce noise
    blurred = cv2.GaussianBlur(normalized, (3, 3), 0)

    # Simple Otsu threshold on normalized image
    _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # For dark LCD displays, digits are lighter than background
    # After Otsu, check if we need to invert for ssocr (expects dark on light)
    if np.mean(thresh) > 127:
        thresh = cv2.bitwise_not(thresh)

    # Save preprocessed image
    crop_path = Path(image_path).parent / "ocr_crop.png"
    cv2.imwrite(str(crop_path), thresh)

    # Run ssocr
    # -d 4: expect up to 4 digits (typical for temp/humidity displays)
    # -t 50: threshold percentage
    try:
        result = subprocess.run(
            ["ssocr", "-d", "4", "-t", "50", str(crop_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

        # Try with 2 digits
        result = subprocess.run(
            ["ssocr", "-d", "2", "-t", "50", str(crop_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

        # Try with inverted image
        thresh_inv = cv2.bitwise_not(thresh)
        cv2.imwrite(str(crop_path), thresh_inv)
        result = subprocess.run(
            ["ssocr", "-d", "2", "-t", "50", str(crop_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

        return None
    except FileNotFoundError:
        print("ssocr not found. Install with: sudo apt install ssocr")
        return None


def capture_image(
    output_path: Path | None = None,
    size: str = DEFAULT_SIZE,
    flip: bool = True,
) -> Path:
    """
    Capture an image from the Arducam IMX477 camera.

    Args:
        output_path: Path to save the image (default: sensors/timed_capture.jpg)
        size: Image size key from AVAILABLE_SIZES
        flip: Whether to rotate image 180 degrees

    Returns:
        Path to the captured image file.
    """
    from picamera2 import Picamera2

    if output_path is None:
        output_path = Path(__file__).parent / "timed_capture.jpg"

    resolution = AVAILABLE_SIZES[size]

    picam2 = Picamera2()
    config = picam2.create_still_configuration(main={"size": resolution})
    picam2.configure(config)
    picam2.start()

    try:
        # Wait for auto-exposure to settle
        time.sleep(2)

        # Capture the image
        picam2.capture_file(str(output_path))

        # Rotate 180 degrees if flip is enabled
        if flip:
            img = Image.open(output_path)
            img = img.rotate(180)
            img.save(output_path)

        return output_path
    finally:
        picam2.stop()
        picam2.close()


def capture_and_ocr(
    output_dir: Path | None = None,
    size: str = DEFAULT_SIZE,
    flip: bool = True,
    crop_region: tuple | None = None,
) -> str | None:
    """
    Capture image and run OCR, returning result or None.

    This is the main function for external use. Captures an image,
    auto-detects or uses provided crop region, and runs OCR.

    Args:
        output_dir: Directory to save captured image (default: sensors/)
        size: Image size key from AVAILABLE_SIZES
        flip: Whether to rotate image 180 degrees
        crop_region: Optional (x, y, w, h) tuple for manual crop region

    Returns:
        OCR result string, or None if no result found.
    """
    if output_dir is None:
        output_dir = Path(__file__).parent

    output_path = output_dir / "timed_capture.jpg"

    # Capture image
    capture_image(output_path=output_path, size=size, flip=flip)

    # Auto-detect or use provided crop region
    if crop_region is None:
        candidates = detect_display(output_path)
        if not candidates:
            return None
        crop_region = candidates[0]

    return run_ocr(output_path, crop_region)


def _parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Capture image from IMX477 camera")
    parser.add_argument(
        "--size",
        "-s",
        choices=AVAILABLE_SIZES.keys(),
        default=DEFAULT_SIZE,
        help=f"Image size to capture (default: {DEFAULT_SIZE})",
    )
    parser.add_argument(
        "--flip",
        "-f",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Rotate image 180 degrees (default: True, use --no-flip to disable)",
    )
    parser.add_argument(
        "--crop",
        "-c",
        type=str,
        default=None,
        metavar="X,Y,W,H",
        help="Crop region for OCR as X,Y,WIDTH,HEIGHT (e.g., 1800,1400,400,200)",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="Run 7-segment OCR (auto-detects display, or use --crop for manual region)",
    )
    parser.add_argument(
        "--debug-detect",
        action="store_true",
        help="Save debug image showing detected display regions",
    )
    parser.add_argument(
        "--cnt",
        type=int,
        default=1,
        help="Number of captures to take (default: 1)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0,
        help="Delay in seconds between captures (default: 0)",
    )
    return parser.parse_args()


def _main():
    """CLI entry point."""
    from picamera2 import Picamera2

    args = _parse_args()
    size = AVAILABLE_SIZES[args.size]

    # Initialize the camera
    picam2 = Picamera2()
    config = picam2.create_still_configuration(main={"size": size})
    picam2.configure(config)
    picam2.start()
    print(f"Using size: {args.size}")

    try:
        print("Waiting for auto-exposure to settle...")
        time.sleep(2)  # Essential for the HQ camera to calibrate light levels

        for capture_num in range(args.cnt):
            if args.cnt > 1:
                print(f"\n=== Capture {capture_num + 1}/{args.cnt} ===")

            # Define file path
            output_path = Path(__file__).parent / "timed_capture.jpg"

            print("Capturing image...")

            # Start the timer
            start_time = time.time()

            # Capture the high-res file
            picam2.capture_file(str(output_path))

            # Calculate elapsed time
            end_time = time.time()
            duration = end_time - start_time

            # Rotate 180 degrees if --flip is enabled (default)
            if args.flip:
                img = Image.open(output_path)
                img = img.rotate(180)
                img.save(output_path)
                print("Image rotated 180 degrees")

            print("-" * 30)
            print("Capture Successful!")
            print(f"File saved to: {output_path}")
            print(f"Time taken to capture and encode: {duration:.2f} seconds")
            print("-" * 30)

            # Run OCR if requested
            if args.ocr:
                if args.crop:
                    # Manual crop region specified
                    crop_region = tuple(map(int, args.crop.split(",")))
                    if len(crop_region) != 4:
                        print("Error: --crop must be X,Y,WIDTH,HEIGHT")
                    else:
                        print(f"Running OCR on region {crop_region}...")

                        if args.debug_detect:
                            # Save debug image with manual crop region
                            debug_img = cv2.imread(str(output_path))
                            x, y, w, h = crop_region
                            cv2.rectangle(
                                debug_img, (x, y), (x + w, y + h), (0, 255, 0), 3
                            )
                            cv2.putText(
                                debug_img,
                                "manual",
                                (x, y - 10),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                1,
                                (0, 255, 0),
                                2,
                            )
                            debug_path = output_path.parent / "debug_detect.jpg"
                            cv2.imwrite(str(debug_path), debug_img)
                            print(f"Debug image saved to: {debug_path}")

                        ocr_result = run_ocr(output_path, crop_region)
                        if ocr_result:
                            print(f"OCR Result: {ocr_result}")
                else:
                    # Auto-detect display
                    print("Auto-detecting 7-segment display...")
                    candidates = detect_display(output_path)

                    if args.debug_detect:
                        # Save debug image with detected regions
                        debug_img = cv2.imread(str(output_path))
                        for i, (x, y, w, h) in enumerate(candidates):
                            color = (0, 255, 0) if i == 0 else (0, 165, 255)
                            cv2.rectangle(
                                debug_img, (x, y), (x + w, y + h), color, 3
                            )
                            cv2.putText(
                                debug_img,
                                f"#{i+1}",
                                (x, y - 10),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                1,
                                color,
                                2,
                            )
                        debug_path = output_path.parent / "debug_detect.jpg"
                        cv2.imwrite(str(debug_path), debug_img)
                        print(f"Debug image saved to: {debug_path}")

                    if candidates:
                        crop_region = candidates[0]  # Use best candidate
                        print(f"Found display at region {crop_region}")
                        ocr_result = run_ocr(output_path, crop_region)
                        if ocr_result:
                            print(f"OCR Result: {ocr_result}")
                    else:
                        print("No 7-segment display detected in image")

            # Delay between captures if specified
            if capture_num < args.cnt - 1 and args.delay > 0:
                print(f"Waiting {args.delay}s before next capture...")
                time.sleep(args.delay)

    finally:
        picam2.stop()
        picam2.close()


if __name__ == "__main__":
    _main()
