from jarvis_cd.launcher.application import Application
from jarvis_cd.comm.mpi_node import MPINode
from jarvis_cd.spack.link_package import LinkSpackage
from jarvis_cd.fs.mkdir_node import MkdirNode
from jarvis_cd.fs.rm_node import RmNode
from jarvis_cd.installer.env_node import EnvNode, EnvNodeOps
from builtin.daos.package import Daos
import configparser
from jarvis_cd.serialize.ini_file import IniFile
import os

class Io500(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.daos = Daos(scaffold_dir=self.config['DAOS']['scaffold']).LoadConfig()

    def _DFSApi(self, pool_uuid, container_uuid, mount, oclass=True):
        cmd = []
        cmd.append(f"DFS")
        cmd.append(f"--dfs.pool={pool_uuid}")
        cmd.append(f"--dfs.cont={container_uuid}")
        cmd.append(f"--dfs.prefix={mount}")
        if oclass:
            cmd.append(f"--dfs.oclass=SX")
        return ' '.join(cmd)

    def _DefineInit(self):
        MkdirNode(self.scaffold_dir, hosts=self.scaffold_hosts).Run()
        MkdirNode(self.config['IO500_ROOT'], hosts=self.scaffold_hosts).Run()
        EnvNode(self.GetEnv(),
            cmd=f"spack load {self.config['IO500_SPACK']}",
            op=EnvNodeOps.SET,
            hosts=self.jarvis_hosts).Run()
        LinkSpackage(self.config['IO500_SPACK'], self.config['IO500_ROOT'], hosts=self.scaffold_hosts).Run()

        #Create io500 sections
        io500_ini = configparser.ConfigParser()
        io500_ini['DEBUG'] = self.config['DEBUG']
        io500_ini['GLOBAL'] = self.config['GLOBAL']
        io500_ini['GLOBAL']['datadir'] = self.config['DAOS']['mount']
        #io500_ini['GLOBAL']['drop-caches-cmd'] = DropCaches().GetCommands()[0]
        io500_ini['ior-easy'] = self.config['ior-easy']
        io500_ini['ior-hard'] = self.config['ior-hard']
        io500_ini['mdtest-easy'] = self.config['mdtest-easy']
        io500_ini['mdtest-hard'] = self.config['mdtest-hard']

        #Get DAOS info
        mount = self.config['DAOS']['mount']
        pool_label = self.config['DAOS']['pool']
        container_label = self.config['DAOS']['container']
        pool_uuid = self.daos.GetPoolUUID(pool_label)
        container_uuid = self.daos.GetContainerUUID(pool_uuid, container_label)

        #Add DAOS API to io500 config
        if 'DAOS' in self.config:
            io500_ini['ior-easy']['API'] = self._DFSApi(pool_uuid, container_uuid, mount)
            io500_ini['ior-hard']['API'] = self._DFSApi(pool_uuid, container_uuid, mount)
            io500_ini['mdtest-easy']['API'] = self._DFSApi(pool_uuid, container_uuid, mount, oclass=False)
            io500_ini['mdtest-hard']['API'] = self._DFSApi(pool_uuid, container_uuid, mount, oclass=False)

        #Create io500 configuration
        IniFile(f"{self.scaffold_dir}/io500.ini").Save(io500_ini)

        #Create Jarvis Cache file
        self.cache = {
            'pool': pool_uuid,
            'container': container_uuid
        }

    def _DefineStart(self):
        os.environ['DAOS_POOL'] = self.cache['pool']
        os.environ['DAOS_CONT'] = self.cache['container']
        os.environ['DAOS_FUSE'] = self.config['DAOS']['mount']
        MPINode(f"{self.config['IO500_ROOT']}/bin/io500 {self.scaffold_dir}/io500.ini", self.config['MPI']['nprocs'], hosts=self.all_hosts).Run()

    def _DefineClean(self):
        paths = [
            f"{self.scaffold_dir}/datafiles",
            f"{self.scaffold_dir}/io500_results",
            f"{self.scaffold_dir}/io500.ini",
            self.GetEnv()
        ]
        RmNode(paths).Run()


    def _DefineStop(self):
        return

    def _DefineStatus(self):
        pass