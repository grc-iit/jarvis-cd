from jarvis_cd import *
from builtin.daos.package import Daos
from builtin.orangefs.package import Orangefs
import configparser
import os

class Io500(Application):
    def _DefineInit(self):
        if 'IO500_SPACK' in self.config:
            LinkSpackage(self.config['IO500_SPACK'], self.config['IO500_ROOT'], hosts=self.all_hosts).Run()
            self.env = [f"spack load {self.config['IO500_SPACK']['package_name']}"]
        elif 'IO500_SCSPKG' in self.config:
            LinkScspkg(self.config['IO500_SCSPKG'], self.config['IO500_ROOT'], hosts=self.all_hosts).Run()
            self.env = [f"module load {self.config['IO500_SCSPKG']}"]

        io500_ini = configparser.ConfigParser()

        #Create basic io500 sections
        if 'DEBUG' in self.config:
            io500_ini['DEBUG'] = self.config['DEBUG']
        if 'GLOBAL' in self.config:
            io500_ini['GLOBAL'] = self.config['GLOBAL']
        # io500_ini['GLOBAL']['drop-caches-cmd'] = DropCaches().GetCommands()[0]
        if 'ior-easy' in self.config:
            io500_ini['ior-easy'] = self.config['ior-easy']
        if 'ior-hard' in self.config:
            io500_ini['ior-hard'] = self.config['ior-hard']
        if 'mdtest-easy' in self.config:
            io500_ini['mdtest-easy'] = self.config['mdtest-easy']
        if 'mdtest-hard' in self.config:
            io500_ini['mdtest-hard'] = self.config['mdtest-hard']
        if 'find' in self.config:
            io500_ini['find'] = self.config['find']

        #Do system-specific initialization functions
        if 'IO500_CLASS' in self.config:
            if self.config['IO500_CLASS'] == 'daos':
                self._DaosInit(io500_ini)
            if self.config['IO500_CLASS'] == 'orangefs':
                self._OrangefsInit(io500_ini)

        #Create io500 configuration
        IniFile(f"{self.shared_dir}/io500.ini").Save(io500_ini)

    def _OrangefsInit(self, io500_ini):
        self.orangefs = Orangefs(pkg_id=self.config['ORANGEFS']['pkg_id'])
        io500_ini['GLOBAL']['datadir'] = os.path.join(self.orangefs.config['CLIENT']['MOUNT_POINT'], 'io500-test-data')
        if 'ORANGEFS_SPACK' in self.config:
            self.env += [
                f"spack load {self.config['ORANGEFS_SPACK']}"
            ]
        elif 'ORANGEFS_SCSPKG' in self.config:
            self.env += [
                f"module load {self.config['ORANGEFS_SCSPKG']}"
            ]

    def _DaosInit(self, io500_ini):
        self.daos = Daos(pkg_id=self.config['DAOS']['pkg_id'])
        if 'DAOS_SPACK' in self.config:
            self.env += [f"spack load {self.config['DAOS_SPACK']['package_name']}"]
        elif 'DAOS_SCSPKG' in self.config:
            self.env += [f"module load {self.config['DAOS_SCSPKG']}"]

        # Get DAOS info
        mount = self.daos.config['CONTAINERS'][0]['mount']
        pool_label = self.config['DAOS']['pool']
        container_label = self.config['DAOS']['container']
        pool_uuid = self.daos.GetPoolUUID(pool_label)
        container_uuid = self.daos.GetContainerUUID(pool_uuid, container_label)

        # Add DAOS API to io500 config
        io500_ini['GLOBAL']['datadir'] = os.path.join(mount, 'io500-test-dir')
        io500_ini['ior-easy']['API'] = self._DFSApi(pool_uuid, container_uuid, mount)
        io500_ini['ior-hard']['API'] = self._DFSApi(pool_uuid, container_uuid, mount)
        io500_ini['mdtest-easy']['API'] = self._DFSApi(pool_uuid, container_uuid, mount, oclass=False)
        io500_ini['mdtest-hard']['API'] = self._DFSApi(pool_uuid, container_uuid, mount, oclass=False)

        self.env += [
            f"export DAOS_POOL={pool_uuid}",
            f"export DAOS_CONT={container_uuid}",
            f"export DAOS_FUSE={mount}",
        ]

    def _DFSApi(self, pool_uuid, container_uuid, mount, oclass=True):
        cmd = []
        cmd.append(f"DFS")
        cmd.append(f"--dfs.pool={pool_uuid}")
        cmd.append(f"--dfs.cont={container_uuid}")
        cmd.append(f"--dfs.prefix={mount}")
        if oclass:
            cmd.append(f"--dfs.oclass=SX")
        return ' '.join(cmd)

    def _DefineStart(self):
        MPINode(f"{self.config['IO500_ROOT']}/bin/io500 {self.shared_dir}/io500.ini",
                self.config['MPI']['nprocs'], hosts=self.all_hosts, collect_output=False).Run()

    def _DefineClean(self):
        paths = [
            f"{self.shared_dir}/datafiles",
            f"{self.shared_dir}/io500_results",
            f"{self.shared_dir}/io500.ini",
            f"{self.shared_dir}/*.jarvis_env.sh"
        ]
        RmNode(paths).Run()
        RmNode(f"{self.per_node_dir}/*", hosts=self.all_hosts).Run()


    def _DefineStop(self):
        KillNode('.*io500.*', hosts=self.all_hosts).Run()

    def _DefineStatus(self):
        pass