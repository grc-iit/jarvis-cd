from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.shell.copy_node import CopyNode
from jarvis_cd.launcher.application import Application
from jarvis_cd.fs.mkdir_node import MkdirNode
from jarvis_cd.fs.rm_node import RmNode
from jarvis_cd.fs.link_node import LinkNode
from jarvis_cd.spack.link_package import LinkSpackage
from jarvis_cd.introspect.detect_networks import DetectNetworks
from jarvis_cd.basic.sleep_node import SleepNode
from jarvis_cd.shell.kill_node import KillNode
from jarvis_cd.basic.echo_node import EchoNode
from jarvis_cd.fs.fs import UnmountFS
from jarvis_cd.serialize.yaml_file import YAMLFile
from jarvis_cd.installer.git_node import GitNode,GitOps
from jarvis_cd.installer.patch_node import PatchNode
from jarvis_cd.hostfile import Hostfile
import os

class Redis(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.server_hosts = self.all_hosts.SelectHosts(self.config['SERVER']['hosts'])
        self.agent_hosts = self.all_hosts.SelectHosts(self.config['AGENT']['hosts'])
        self.control_hosts = self.all_hosts.SelectHosts(self.config['CONTROL']['hosts'])
        self.all_hosts = self.all_hosts
        if 'DAOS_HOSTS' in self.config:
            self.all_hosts = Hostfile().LoadHostfile(self.config['DAOS_HOSTS'])
        self.server_hosts = self.all_hosts.SelectHosts(self.config['SERVER']['hosts'])
        self.agent_hosts = self.all_hosts.SelectHosts(self.config['AGENT']['hosts'])
        self.control_hosts = self.all_hosts.SelectHosts(self.config['CONTROL']['hosts'])
        self.pools_by_label = {}

    def Install(self):
        ExecNode(f"spack install redis", hosts=self.all_hosts).Run()

    def _DefineInit(self):
        pass

    def _DefineStart(self):
        "redis-server"
        pass

    def _DefineClean(self):
        pass

    def _DefineStop(self):
        pass

    def _DefineStatus(self):
        pass