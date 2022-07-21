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
import os

class Memcached(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()

    def _DefineInit(self):
        #Create SCAFFOLD on all nodes
        MkdirNode(self.scaffold_dir, hosts=self.scaffold_hosts).Run()

    def _DefineStart(self):
        pass

    def _DefineClean(self):
        pass

    def _DefineStop(self):
        pass

    def _DefineStatus(self):
        pass