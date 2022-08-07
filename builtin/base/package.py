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
        RmNode(os.path.join(self.GetUserEnv(), '*'), hosts=self.all_hosts, shell=True).Run()

    def Cd(self, pkg_id):
        self._PackagePathsFromID(pkg_id)
        if self.shared_exists:
            print(self.shared_dir)
            return self.shared_dir
        elif self.per_node_exists:
            print(self.per_node_dir)
            return self.per_node_dir
        return None
    def _CdArgs(self, parser):
        parser.add_argument('pkg_id', metavar='pkg_id', type=str, help='The id of the package to cd into')

    def CdShared(self, pkg_id):
        self._PackagePathsFromID(pkg_id)
        if self.shared_exists:
            print(self.shared_dir)
        return self.shared_dir
    def _CdSharedArgs(self, parser):
        self._CdArgs(parser)

    def CdPerNode(self, pkg_id):
        self._PackagePathsFromID(pkg_id)
        if self.per_node_exists:
            print(self.per_node_dir)
        return self.per_node_dir
    def _CdPerNodeArgs(self, parser):
        self._CdArgs(parser)

    def Rm(self, pkg_id):
        self._PackagePathsFromID(pkg_id)
        RmNode(self.shared_dir).Run()
        RmNode(self.per_node_dir, hosts=self.all_hosts).Run()
    def _RmArgs(self, parser):
        self._CdArgs(parser)

    def List(self):
        pkgs = set()
        for pkg in os.listdir(self.jarvis_shared_pkg_dir):
            pkgs.add(pkg)
        for pkg in os.listdir(self.jarvis_per_node_pkg_dir):
            pkgs.add(pkg)
        for pkg in pkgs:
            print(pkg)