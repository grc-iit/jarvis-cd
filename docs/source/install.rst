Install
=======

To install Jarvis:

``
cd jarvis-cd
bash dependencies.sh
source ~/.bashrc
python3 -m pip install -e . --user -r requirements.txt
jarvis deps scaffold local
jarvis deps local-install all
source ~/.bashrc
``
