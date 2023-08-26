#!/bin/bash
pushd ${GITHUB_WORKSPACE} || exit
git clone https://github.com/scs-lab/jarvis-util.git
pushd jarvis-util || exit
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
popd || exit
popd || exit

python3 -m pip install -r requirements.txt
python3 -m pip install -e .

jarvis init "${HOME}/jarvis-pipelines" "${HOME}/jarvis-pipelines" "${HOME}/jarvis-pipelines"