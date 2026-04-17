#!/bin/bash
set -e

# PyTorch matching CUDA 12.6 (cached after first install)
pip3 install --break-system-packages -q \
        torch torchvision torchaudio \
        --index-url https://download.pytorch.org/whl/cu126 \
    && pip3 install --break-system-packages -q \
        numpy matplotlib tensorboard
