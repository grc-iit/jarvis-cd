from jarvis_cd.launchers.launcher import Launcher
from jarvis_cd.fs.fs import DropCaches
from jarvis_cd.comm.mpi_node import MPINode
from jarvis_cd.spack.link_package import LinkSpackage
from jarvis_cd.basic.mkdir_node import MkdirNode
import configparser

#POOL_ID: 564cf1ad-959e-4e52-9707-290b48d7e728
#CONTAINER_ID: 29866e67-baac-4886-8e76-ce0eb6659b95

#Get POOL ID: dmg pool list --verbose -o daos_control.yaml
#Get container ID: daos cont list io500_pool

class Io500(Launcher):
    def __init__(self, config_path=None, args=None):
        super().__init__('io500', config_path, args)

    def _ProcessConfig(self):
        super()._ProcessConfig()

    def _DFSApi(self, daos, oclass=True):
        cmd = []
        cmd.append(f"DFS")
        cmd.append(f"--dfs.pool={daos['pool']}")
        cmd.append(f"--dfs.cont={daos['container']}")
        cmd.append(f"--dfs.prefix={daos['mount']}")
        if oclass:
            cmd.append(f"--dfs.oclass=SX")
        return ' '.join(cmd)

    def _DefineInit(self):
        MkdirNode(self.scaffold_dir, hosts=self.scaffold_hosts, ssh_info=self.ssh_info).Run()
        MkdirNode(self.config['IO500_ROOT'], hosts=self.scaffold_hosts, ssh_info=self.ssh_info).Run()
        LinkSpackage(self.config['IO500_SPACK'], self.config['IO500_ROOT'], hosts=self.scaffold_hosts, ssh_info=self.ssh_info).Run()
        io500_ini = configparser.ConfigParser()
        io500_ini['DEBUG'] = self.config['DEBUG']
        io500_ini['GLOBAL'] = self.config['GLOBAL']
        io500_ini['GLOBAL']['datadir'] = self.config['DAOS']['mount']
        #io500_ini['GLOBAL']['drop-caches-cmd'] = DropCaches().GetCommands()[0]
        io500_ini['ior-easy'] = self.config['ior-easy']
        io500_ini['ior-hard'] = self.config['ior-hard']
        io500_ini['mdtest-easy'] = self.config['mdtest-easy']
        io500_ini['mdtest-hard'] = self.config['mdtest-hard']
        if 'DAOS' in self.config:
            io500_ini['ior-easy']['API'] = self._DFSApi(self.config['DAOS'])
            io500_ini['ior-hard']['API'] = self._DFSApi(self.config['DAOS'])
            io500_ini['mdtest-easy']['API'] = self._DFSApi(self.config['DAOS'], oclass=False)
            io500_ini['mdtest-hard']['API'] = self._DFSApi(self.config['DAOS'], oclass=False)
        with open(f"{self.scaffold_dir}/io500.ini", 'w') as fp:
            io500_ini.write(fp)

    def _DefineStart(self):
        MPINode(f"{self.config['IO500_ROOT']}/bin/io500 {self.scaffold_dir}/io500.ini", self.config['MPI']['nprocs'], hosts=self.all_hosts).Run()

    def _DefineClean(self):
        return

    def _DefineStop(self):
        return

    def _DefineStatus(self):
        pass