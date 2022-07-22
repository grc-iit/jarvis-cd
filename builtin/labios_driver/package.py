from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.shell.copy_node import CopyNode
from jarvis_cd.launcher.application import Application
from jarvis_cd.fs.mkdir_node import MkdirNode
from jarvis_cd.fs.rm_node import RmNode
from jarvis_cd.spack.link_package import LinkSpackage
from jarvis_cd.shell.kill_node import KillNode
from jarvis_cd.mpi.mpi_node import MPINode
from jarvis_repos.builtin.memcached.package import Memcached
from jarvis_repos.builtin.nats_server.package import NatsServer
from jarvis_cd.serialize.yaml_file import YAMLFile
import os

class LabiosDriver(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()
        test_cases = {
            'SIMPLE_WRITE': 0,
            'SIMPLE_READ': 1,
            'CM1_BASE': 2,
            'CM1_TABIOS': 3,
            'MONTAGE_BASE': 4,
            'MONTAGE_TABIOS': 5,
            'HACC_BASE': 6,
            'HACC_TABIOS': 7,
            'KMEANS_BASE': 8,
            'KMEANS_TABIOS': 9,
            'STRESS_TEST': 10,
        }
        self.test_case = test_cases[self.config['TEST_CASE']]

    def _DefineInit(self):
        pass

    def _DefineStart(self):
        MPINode(f"labios_driver {self.config['LABIOS_CONF']} {self.test_case}",
                self.config['NPROCS'],
                hosts=self.all_hosts).Run()

    def _DefineClean(self):
        pass

    def _DefineStop(self):
        pass

    def _DefineStatus(self):
        pass