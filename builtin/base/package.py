import sys
from jarvis_cd.launcher.launcher import Launcher
from jarvis_cd.fs.rm_node import RmNode
import os

class Base(Launcher):
    def Exec(self, cmd):
        exec(cmd)
    def _ExecArgs(self, parser):
        parser.add_argument('cmd', metavar='command', type=str, help='The python code to execute')

    def CheckEnv(self):
        for item in os.listdir(self.GetUserEnv()):
            print(item)

    def UnloadEnv(self):
        RmNode(os.path.join(self.GetUserEnv(), '*'), hosts=self.all_hosts, shell=True).Run()

    def Cd(self, pkg_id, p):
        self._PackagePathsFromID(pkg_id)
        if not p:
            print(self.shared_dir)
            return self.shared_dir
        else:
            print(self.per_node_dir)
            return self.per_node_dir
    def _CdArgs(self, parser):
        parser.add_argument('pkg_id', metavar='pkg_id', type=str, help='The id of the package to cd into')
        parser.add_argument('-p', action="store_true", help='Path of the shared directory')

    def Rm(self, pkg_id):
        self._PackagePathsFromID(pkg_id)
        RmNode(self.shared_dir).Run()
        RmNode(self.per_node_dir, hosts=self.all_hosts).Run()
    def _RmArgs(self, parser):
        parser.add_argument('pkg_id', metavar='pkg_id', type=str, help='The id of the package to cd into')

    def List(self):
        pkgs = set()
        for pkg in os.listdir(self.jarvis_shared_pkg_dir):
            pkgs.add(pkg)
        for pkg in os.listdir(self.jarvis_per_node_pkg_dir):
            pkgs.add(pkg)
        for pkg in pkgs:
            print(pkg)