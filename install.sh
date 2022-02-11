
JARVIS_CD_ROOT=`pwd`
JARVIS_CD_TMP="/tmp/${USER}/jarvis_cd"
echo "export JARVIS_CD_ROOT=${JARVIS_CD_ROOT}" >> ~/.bashrc
echo "export JARVIS_CD_TMP=${JARVIS_CD_TMP}" >> ~/.bashrc
echo "export PYTHONPATH=\${JARVIS_CD_ROOT}/src:\${JARVIS_CD_ROOT}/repos:$PYTHONPATH" >> ~/.bashrc
echo "export PATH=\${JARVIS_CD_ROOT}/bin:$PATH" >> ~/.bashrc
chmod +x bin/jarvis
mkdir -p ${JARVIS_CD_TMP}

python3 -m pip install -r requirements.txt