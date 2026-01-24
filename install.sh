#!/bin/bash

if [ -d ".venv" ]; then
    echo "Activating existing virtual environment..."
    source .venv/bin/activate
else
    echo "Installing GPIO library..."
    sudo apt install python3-rpi-lgpio

    echo "Creating new virtual environment..."
    # use system packages for python3-rpi-lgpio access
    python3 -m venv --system-site-packages .venv
    source .venv/bin/activate
    echo "Updating pip..."
    pip install --upgrade pip
    echo "Installing requirements..."
    pip install -r requirements.txt
fi