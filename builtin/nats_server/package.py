from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.launcher.application import Application
from jarvis_cd.fs.mkdir_node import MkdirNode
from jarvis_cd.fs.rm_node import RmNode
from jarvis_cd.fs.link_node import LinkNode
from jarvis_cd.spack.link_package import LinkSpackage
from jarvis_cd.shell.kill_node import KillNode

class NatsServer(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()

    def _DefineInit(self):
        #Create SCAFFOLD on all nodes
        MkdirNode(self.scaffold_dir, hosts=self.scaffold_hosts).Run()
        #Create link to NATS spackage
        LinkSpackage(self.config['NATS_SPACK'], self.config['NATS_ROOT'], hosts=self.scaffold_hosts).Run()

    def _DefineStart(self):
        nats_start_cmd = [
            f"{self.config['NATS_ROOT']}/bin/nats-server",
            f"-p {self.config['PORT']}",
            f"-a {self.all_hosts.list()[0]}",
            f"-DV",
            f"-l {self.config['LOG_PATH']}",
        ]
        nats_start_cmd = " ".join(nats_start_cmd)
        ExecNode(nats_start_cmd, exec_async=True).Run()

    def _DefineClean(self):
        pass

    def _DefineStop(self):
        KillNode('.*nats-server.*', hosts=self.all_hosts)

    def _DefineStatus(self):
        pass