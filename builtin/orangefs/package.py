from jarvis_cd import *
import os

class Orangefs(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.server_hosts = self.all_hosts.SelectHosts(self.config['SERVER']['HOSTS'])
        self.md_hosts = self.all_hosts.SelectHosts(self.config['METADATA']['HOSTS'])
        self.client_hosts = self.all_hosts.SelectHosts(self.config['CLIENT']['HOSTS'])

        self.orangefs_root = os.path.join(self.per_node_dir, 'orangefs_install')
        self.pfs_conf = os.path.join(self.shared_dir, "orangefs.conf")

    def _DefineInit(self):
        #Link orangefs spackage
        if 'ORANGEFS_SPACK' in self.config:
            LinkSpackage(self.config['ORANGEFS_SPACK'], self.orangefs_root, hosts=self.all_hosts).Run()
        elif 'ORANGEFS_SCSPKG' in self.config:
            LinkScspkg(self.config['ORANGEFS_SCSPKG'], self.orangefs_root, hosts=self.all_hosts).Run()
            self.env = [f"module load {self.config['ORANGEFS_SCSPKG']}"]

        # generate PFS Gen config
        pvfs_gen_cmd = []
        pvfs_gen_cmd.append(f"{os.path.join(self.orangefs_root, 'bin', 'pvfs2-genconfig')}")
        pvfs_gen_cmd.append(f"--quiet")
        pvfs_gen_cmd.append(f"--protocol {self.config['SERVER']['PVFS2_PROTOCOL']}")
        if self.config['SERVER']['PVFS2_PROTOCOL'] == 'tcp':
            pvfs_gen_cmd.append(f"--tcpport {self.config['SERVER']['PVFS2_PORT']}")
        elif self.config['SERVER']['PVFS2_PROTOCOL'] == 'ib':
            pvfs_gen_cmd.append(f"--ibport {self.config['SERVER']['PVFS2_PORT']}")
        pvfs_gen_cmd.append(f"--dist-name {self.config['SERVER']['DISTRIBUTION_NAME']}")
        pvfs_gen_cmd.append(f"--dist-params strip_size: {self.config['SERVER']['STRIPE_SIZE']}")
        pvfs_gen_cmd.append(f"--ioservers {self.server_hosts.to_str(sep=',')}")
        pvfs_gen_cmd.append(f"--metaservers {self.md_hosts.to_str(sep=',')}")
        pvfs_gen_cmd.append(f"--storage {self.config['SERVER']['STORAGE_DIR']}")
        pvfs_gen_cmd.append(f"--metadata {self.config['METADATA']['META_DIR']}")
        pvfs_gen_cmd.append(f"--logfile {self.config['SERVER']['LOG']}")
        pvfs_gen_cmd.append(self.pfs_conf)
        pvfs_gen_cmd = " ".join(pvfs_gen_cmd)
        ExecNode(pvfs_gen_cmd).Run()
        CopyNode(self.pfs_conf, hosts=self.shared_hosts).Run()

        #Create storage directories
        MkdirNode(self.config['CLIENT']['MOUNT_POINT'], hosts=self.client_hosts).Run()
        MkdirNode(self.config['SERVER']['STORAGE_DIR'], hosts=self.server_hosts).Run()
        MkdirNode(self.config['METADATA']['META_DIR'], hosts=self.md_hosts).Run()

        #Prepare storage
        if 'PREPARE_STORAGE' in self.config:
            PrepareStorage(self.config['PREPARE_STORAGE'], hosts=self.server_hosts).Run()

        #set pvfstab on clients
        for i,client in self.client_hosts.enumerate():
            metadata_server_ip = self.md_hosts.list()[i % len(self.md_hosts)]
            cmd = "echo '{protocol}://{ip}:{port}/orangefs {mount_point} pvfs2 defaults,auto 0 0' > {client_pvfs2tab}".format(
                protocol=self.config['SERVER']['PVFS2_PROTOCOL'],
                port=self.config['SERVER']['PVFS2_PORT'],
                ip=metadata_server_ip,
                mount_point=self.config['CLIENT']['MOUNT_POINT'],
                client_pvfs2tab=self.config['CLIENT']['PVFS2TAB']
            )
            ExecNode(cmd, hosts=client, shell=True).Run()

    def _DefineStart(self):
        # start pfs servers
        for host in self.server_hosts:
            pvfs2_server = os.path.join(self.orangefs_root,"sbin","pvfs2-server")
            server_start_cmds = [
                f"{pvfs2_server} {self.pfs_conf} -f -a {host}",
                f"{pvfs2_server} {self.pfs_conf} -a {host}"
            ]
            ExecNode(server_start_cmds, hosts=host).Run()
        SleepNode(5).Run()
        self.Status()

        # start pfs client
        pvfs2_fuse = os.path.join(self.orangefs_root, "bin", "pvfs2fuse")
        for i,client in self.client_hosts.enumerate():
            metadata_server_ip = self.md_hosts.list()[i % len(self.md_hosts)]
            start_client_cmds = [
                "{pvfs2_fuse} -o fs_spec={protocol}://{ip}:{port}/orangefs {mount_point}".format(
                    pvfs2_fuse=pvfs2_fuse,
                    protocol=self.config['SERVER']['PVFS2_PROTOCOL'],
                    port=self.config['SERVER']['PVFS2_PORT'],
                    ip=metadata_server_ip,
                    mount_point=self.config['CLIENT']['MOUNT_POINT'])
            ]
            ExecNode(start_client_cmds, hosts=client).Run()

    def _DefineStop(self):
        cmds = [
            f"umount -l {self.config['CLIENT']['MOUNT_POINT']}",
            f"umount -f {self.config['CLIENT']['MOUNT_POINT']}",
            f"umount {self.config['CLIENT']['MOUNT_POINT']}",
            f"killall -9 pvfs2-client",
            f"killall -9 pvfs2-client-core"
        ]
        ExecNode(cmds, hosts=self.client_hosts, sudo=True).Run()
        ExecNode("killall -9 pvfs2-server", sudo=True, hosts=self.server_hosts).Run()
        ExecNode("pgrep -la pvfs2-server", hosts=self.client_hosts).Run()

    def _DefineClean(self):
        RmNode(self.config['CLIENT']['MOUNT_POINT'], hosts=self.client_hosts).Run()
        RmNode(self.config['SERVER']['STORAGE_DIR'], hosts=self.server_hosts).Run()
        RmNode(self.config['METADATA']['META_DIR'], hosts=self.md_hosts).Run()
        RmNode(self.orangefs_root, hosts=self.all_hosts).Run()
        if 'PREPARE_STORAGE' in self.config:
            UnprepareStorage(self.config['PREPARE_STORAGE'], hosts=self.server_hosts).Run()

    def _DefineStatus(self):
        ExecNode("mount | grep pvfs", hosts=self.server_hosts, shell=True).Run()
        pvfs2_ping = os.path.join(self.orangefs_root, "bin", "pvfs2-ping")
        verify_server_cmd = [
            f"export LD_LIBRARY_PATH={os.path.join(self.orangefs_root, 'lib')}",
            f"export PVFS2TAB_FILE={self.config['CLIENT']['PVFS2TAB']}",
            f"{pvfs2_ping} -m {self.config['CLIENT']['MOUNT_POINT']} | grep 'appears to be correctly configured'"
        ]
        verify_server_cmd = ';'.join(verify_server_cmd)
        ExecNode(verify_server_cmd, hosts=self.client_hosts, shell=True).Run()