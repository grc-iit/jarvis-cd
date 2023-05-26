#!/bin/bash
git clone https://github.com/scs-lab/jarvis-util.git
pushd jarvis-util || exit
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
popd || exit

python3 -m pip install -r requirements.txt
python3 -m pip install -e .
