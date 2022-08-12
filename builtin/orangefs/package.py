from jarvis_cd.basic.echo_node import EchoNode
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.launcher.application import Application
import os

from jarvis_cd.spack.link_package import LinkSpackage
from jarvis_cd.basic.sleep_node import SleepNode
from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.shell.copy_node import CopyNode
from jarvis_cd.fs.mkdir_node import MkdirNode
from jarvis_cd.fs.rm_node import RmNode

class Orangefs(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.server_hosts = self.all_hosts.SelectHosts(self.config['SERVER']['HOSTS'])
        self.md_hosts = self.all_hosts.SelectHosts(self.config['METADATA']['HOSTS'])
        self.client_hosts = self.all_hosts.SelectHosts(self.config['CLIENT']['HOSTS'])

        self.orangefs_root = os.path.join(self.per_node_dir, 'orangefs_install')
        self.pfs_conf = os.path.join(self.shared_dir,"pfs_{}.conf".format(len(self.server_hosts)))

    def _DefineInit(self):
        #Link orangefs spackage
        LinkSpackage(self.config['ORANGEFS_SPACK'], self.orangefs_root, hosts=self.all_hosts).Run()

        # generate PFS Gen config
        pvfs_gen_cmd = []
        pvfs_gen_cmd.append(f"{os.path.join(self.orangefs_root, 'bin', 'pvfs2-genconfig')}")
        pvfs_gen_cmd.append(f"--quiet")
        pvfs_gen_cmd.append(f"--protocol {self.config['SERVER']['PVFS2_PROTOCOL']}")
        pvfs_gen_cmd.append(f"--tcpport {self.config['SERVER']['PVFS2_PORT']}")
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
        CopyNode(self.pfs_conf, self.pfs_conf, hosts=self.server_hosts).Run()

        #set pvfstab on clients
        for i,client in self.client_hosts.enumerate():
            metadata_server_ip = self.md_hosts.list()[i % len(self.md_hosts)]
            cmd.append("echo '{protocol}://{ip}:{port}/orangefs {mount_point} pvfs2 defaults,auto 0 0' > {client_pvfs2tab}".format(
                protocol=self.config['SERVER']['PVFS2_PROTOCOL'],
                port=self.config['SERVER']['PVFS2_PORT'],
                ip=metadata_server_ip,
                mount_point=self.config['CLIENT']['MOUNT_POINT'],
                client_pvfs2tab=self.config['CLIENT']['PVFS2TAB']
            ))
            ExecNode(cmds, hosts=client, shell=True).Run()

        #Create directories
        MkdirNode(self.config['CLIENT']['MOUNT_POINT'], hosts=self.client_hosts).Run()
        MkdirNode(self.config['SERVER']['STORAGE_DIR'], hosts=self.server_hosts).Run()

    def _DefineStart(self):
        # start pfs servers
        pvfs2_server = os.path.join(self.orangefs_root,"sbin","pvfs2-server")
        server_start_cmds = [
            f"{pvfs2_server} {self.pfs_conf} -f -a {host}",
            f"{pvfs2_server} {self.pfs_conf} -a {host}"
        ]
        ExecNode(server_start_cmds, hosts=self.server_hosts).Run()
        SleepNode(5).Run()
        self.Status()

        # start pfs client
        #kernel_ko = os.path.join(self.orangefs_root, "lib/modules/3.10.0-862.el7.x86_64/kernel/fs/pvfs2/pvfs2.ko")
        pvfs2_client = os.path.join(self.orangefs_root, "sbin","pvfs2-client")
        pvfs2_client_core = os.path.join(self.orangefs_root, "sbin", "pvfs2-client-core")
        for i,client in self.client_hosts.enumerate():
            metadata_server_ip = self.md_hosts.list()[i % len(self.md_hosts)]
            start_client_cmds = [
                #"sudo insmod {}".format(kernel_ko),
                "sudo {} -p {}".format(pvfs2_client, pvfs2_client_core),
                "sudo mount -t pvfs2 {protocol}://{ip}:{port}/orangefs {mount_point}".format(
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
            f"killall -9 pvfs2-client-core",
            f"rmmod pvfs2",
            f"kill-pvfs2-client"
        ]
        ExecNode(cmds, hosts=self.client_hosts, sudo=True).Run()
        ExecNode("killall -9 pvfs2-server", hosts=self.server_hosts).Run()
        ExecNode("pgrep -la pvfs2-server", hosts=self.client_hosts).Run()

    def _DefineClean(self):
        RmNode(self.config['CLIENT']['MOUNT_POINT'], hosts=self.client_hosts).Run()
        RmNode(self.config['SERVER']['STORAGE_DIR'], hosts=self.server_hosts).Run()

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