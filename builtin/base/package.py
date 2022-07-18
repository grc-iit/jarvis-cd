import sys
from jarvis_cd.launcher.launcher import Launcher
from jarvis_cd.fs.rm_node import RmNode
import os

class Base(Launcher):
    def Exec(self, cmd):
        exec(cmd)
    def _ExecArgs(self, parser):
        parser.add_argument('cmd', metavar='command', type=str, help='The python code to execute')

    def UnloadEnv(self):
        RmNode(os.path.join(self.GetUserEnv(), '*'), hosts=self.jarvis_hosts, shell=True).Run()