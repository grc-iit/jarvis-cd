from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.shell.copy_node import CopyNode
from jarvis_cd.launcher.application import Application
from jarvis_cd.fs.mkdir_node import MkdirNode
from jarvis_cd.fs.rm_node import RmNode
from jarvis_cd.spack.link_package import LinkSpackage
from jarvis_cd.shell.kill_node import KillNode

class Memcached(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.memcached_host = self.all_hosts.SelectHosts(self.config['MEMCACHED_HOST'])

    def _DefineInit(self):
        #Create SCAFFOLD on all nodes
        MkdirNode(self.scaffold_dir, hosts=self.scaffold_hosts).Run()
        #Create link to memcached spackage
        LinkSpackage(self.config['MEMCACHED_SPACK'], self.config['MEMCACHED_ROOT'], hosts=self.scaffold_hosts).Run()

    def _DefineStart(self):
        start_memcached_cmd = [
            f"{self.config['MEMCACHED_ROOT']}/bin/memcached",
            f"-p {self.config['PORT']}",
            f"-l {self.memcached_host.list()[0]}",
            f"-d",
            f"-I {self.config['MAX_ITEM_SIZE']}"
        ]
        start_memcached_cmd = " ".join(start_memcached_cmd)
        ExecNode(start_memcached_cmd).Run()

    def _DefineClean(self):
        pass

    def _DefineStop(self):
        KillNode('.*memcached.*').Run()

    def _DefineStatus(self):
        pass