#!/bin/bash
JARVIS_CD_ROOT=`pwd`
JARVIS_CD_TMP="/tmp/${USER}/jarvis_cd"

scspkg create jarvis-cd
scspkg set-env jarvis-cd JARVIS_CD_ROOT `pwd`
scspkg set-env jarvis-cd JARVIS_CD_TMP ${JARVIS_CD_TMP}
scspkg prepend-env jarvis-cd PYTHONPATH ${JARVIS_CD_ROOT}
scspkg prepend-env jarvis-cd PATH ${JARVIS_CD_ROOT}/bin

chmod +x bin/jarvis
mkdir -p ${JARVIS_CD_TMP}
python3 -m pip install -r requirements.txt
echo "module load jarvis-cd" >> ~/.bashrc
