#!/bin/bash

if [ -d ".venv" ]; then
    echo "Activating existing virtual environment..."
    source .venv/bin/activate
else
    echo "Creating new virtual environment..."
    python3 -m venv .venv
    source .venv/bin/activate
    echo "Updating pip..."
    pip install --upgrade pip
    echo "Installing requirements..."
    pip install -r requirements.txt
fi