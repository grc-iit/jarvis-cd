#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

ARLDM_REPO=https://github.com/xichenpan/ARLDM.git
ARLDM_PATH=/opt/ARLDM

apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git \
        python3 python3-pip \
        openmpi-bin libopenmpi-dev \
    && rm -rf /var/lib/apt/lists/*

# ARLDM source. The training entry point is /opt/ARLDM/main.py (Hydra +
# PyTorch Lightning); data prep scripts live under data_script/.
git clone --depth 1 "${ARLDM_REPO}" "${ARLDM_PATH}"

# CPU-only PyTorch + the exact ML libs ARLDM's main.py imports. The CPU
# wheel is ~200 MB vs the ~2 GB CUDA wheel — enough to drive Trainer.fit()
# on a tiny module, which is what start() runs.
# Pin pytorch_lightning < 2.3 for a stable pl.LightningModule API.
pip3 install --upgrade 'pip>=23.1'
pip3 install --index-url https://download.pytorch.org/whl/cpu \
    'torch>=2.0,<2.3'
pip3 install \
    'pytorch-lightning>=2.0,<2.3' \
    'transformers>=4.30,<4.45' \
    'numpy<2.0'
