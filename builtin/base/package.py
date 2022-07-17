import sys
from jarvis_cd.launcher.launcher import Launcher

class Base(Launcher):
    def Exec(self, cmd):
        exec(cmd)
    def _ExecArgs(self, parser):
        parser.add_argument('cmd', metavar='command', type=str, help='The python code to execute')