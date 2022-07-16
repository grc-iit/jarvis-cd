from jarvis_cd.basic.echo_node import EchoNode
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.launchers import Launcher
import os
import socket

from jarvis_cd.shell.scp_node import SCPNode
from jarvis_cd.basic.sleep_node import SleepNode
from jarvis_cd.shell.exec_node import ExecNode

class Orangefs(Launcher):
    def __init__(self, config_path=None, args=None):
        super().__init__('orangefs', config_path, args)

    def _ProcessConfig(self):
        self.server_data_hosts = Hostfile().LoadHostfile(self.config['SERVER']['SERVER_DATA_HOST_FILE'])
        self.server_meta_hosts = Hostfile().LoadHostfile(self.config['SERVER']['SERVER_META_HOST_FILE'])
        self.client_hosts = Hostfile().LoadHostfile(self.config['CLIENT']['CLIENT_HOST_FILE'])

        self.pvfs_genconfig = os.path.join(self.config["COMMON"]["ORANGEFS_INSTALL_DIR"], "bin", "pvfs2-genconfig")
        self.pfs_conf = os.path.join(self.scaffold_dir,"pfs_{}.conf".format(len(self.server_data_hosts)))
        self.pfs_conf_scp = "{}_src".format(self.pfs_conf)

    def SetNumHosts(self, num_data_hosts, num_meta_hosts, num_client_hosts):
        self.server_data_hosts.SelectHosts(num_data_hosts)
        self.server_meta_hosts.SelectHosts(num_meta_hosts)
        self.client_hosts.SelectHosts(num_client_hosts)
        return

    def _DefineInit(self):
        # generate PFS Gen config
        pvfs_gen_cmd = []
        pvfs_gen_cmd.append(f"{self.pvfs_genconfig}")
        pvfs_gen_cmd.append(f"--quiet")
        pvfs_gen_cmd.append(f"--protocol {self.config['SERVER']['PVFS2_PROTOCOL']}")
        pvfs_gen_cmd.append(f"--tcpport {self.config['SERVER']['PVFS2_PORT']}")
        pvfs_gen_cmd.append(f"--dist-name {self.config['SERVER']['PVFS2_DISTRIBUTION_NAME']}")
        pvfs_gen_cmd.append(f"--dist-params strip_size: {self.config['SERVER']['PVFS2_STRIP_SIZE']}")
        pvfs_gen_cmd.append(f"--ioservers {self.server_data_hosts.to_str(sep=',')}")
        pvfs_gen_cmd.append(f"--metaservers {self.server_meta_hosts.to_str(sep=',')}")
        pvfs_gen_cmd.append(f"--storage {os.path.join(self.config['SERVER']['SERVER_LOCAL_STORAGE_DIR'],'data')}")
        pvfs_gen_cmd.append(f"--metadata {os.path.join(self.config['SERVER']['SERVER_LOCAL_STORAGE_DIR'],'meta')}")
        pvfs_gen_cmd.append(f"--logfile {os.path.join(self.config['SERVER']['SERVER_LOCAL_STORAGE_DIR'],'orangefs.log')}")
        pvfs_gen_cmd.append(self.pfs_conf_scp)
        pvfs_gen_cmd = " ".join(pvfs_gen_cmd)
        ExecNode(pvfs_gen_cmd).Run()

        # set pvfstab on clients
        for i,client in self.client_hosts.enumerate():
            metadata_server = self.server_meta_hosts[i % len(self.server_meta_hosts)]
            metadata_server_ip = socket.gethostbyname(metadata_server)
            cmd.append("echo '{protocol}://{ip}:{port}/orangefs {mount_point} pvfs2 defaults,auto 0 0' > {client_pvfs2tab}".format(
                protocol=self.config['SERVER']['PVFS2_PROTOCOL'],
                port=self.config['SERVER']['PVFS2_PORT'],
                ip=metadata_server_ip,
                mount_point=self.config['CLIENT']['CLIENT_MOUNT_POINT_DIR'],
                client_pvfs2tab=self.config['CLIENT']['CLIENT_PVFS2TAB_FILE']
            ))
            cmd.append("mkdir -p {}".format(self.config['CLIENT']['CLIENT_MOUNT_POINT_DIR']))
            node = ExecNode("set pvfstab for client {}".format(metadata_server),client,cmds)
            node.Run()

        ## create tmp dir
        tmp_dir_node = ExecNode("make tmp dir in clients",self.client_hosts,"mkdir -p {}".format(self.temp_dir))
        tmp_dir_node.Run()
        ## copy pfs conf
        copy_node = SCPNode("cp conf file",self.server_data_hosts,self.pfs_conf_scp,self.pfs_conf)
        copy_node.Run()

        ## create server data storage
        ssh_dir_node = ExecNode("make data dir in server",self.server_data_hosts,"mkdir -p {}".format(self.config['SERVER']['SERVER_LOCAL_STORAGE_DIR']))
        ssh_dir_node.Run()

    def _DefineStart(self):
        nodes = []

        # start pfs servers
        pvfs2_server = os.path.join(self.config['COMMON']['ORANGEFS_INSTALL_DIR'],"sbin","pvfs2-server")
        pvfs2_ping = os.path.join(self.config['COMMON']['ORANGEFS_INSTALL_DIR'],"bin","pvfs2-ping")
        for host in self.server_data_hosts:
            server_start_cmds =[
                "{pfs_server} {pfs_conf} -f -a {host}".format(pfs_server=pvfs2_server, pfs_conf=self.pfs_conf, host=host),
                "{pfs_server} {pfs_conf} -a {host}".format(pfs_server=pvfs2_server, pfs_conf=self.pfs_conf, host=host)
            ]
            start_pfs_servers = ExecNode("start pfs servers",host,server_start_cmds)
            start_pfs_servers.Run()

        #Verify
        SleepNode("sleep timer",5,print_output=True).Run()
        EchoNode("verify server printing","Verifying OrangeFS servers ...").Run()
        verify_server_cmd = "export LD_LIBRARY_PATH={pvfs2_lib}; export PVFS2TAB_FILE={client_pvfs2tab}; " \
                            "{pvfs2_ping} -m {mount_point} | grep 'appears to be correctly configured'".format(
            pvfs2_lib=os.path.join(self.config['COMMON']['ORANGEFS_INSTALL_DIR'],"lib"),
            client_pvfs2tab=self.config['CLIENT']['CLIENT_PVFS2TAB_FILE'],
            pvfs2_ping=pvfs2_ping,
            mount_point=self.config['CLIENT']['CLIENT_MOUNT_POINT_DIR']
        )
        verify_server = ExecNode("verify server",self.client_hosts,verify_server_cmd,print_output=True)
        verify_server.Run()

        # start pfs client
        kernel_ko = os.path.join(self.config['COMMON']['ORANGEFS_INSTALL_DIR'], "lib/modules/3.10.0-862.el7.x86_64/kernel/fs/pvfs2/pvfs2.ko")
        pvfs2_client = os.path.join(self.config['COMMON']['ORANGEFS_INSTALL_DIR'], "sbin","pvfs2-client")
        pvfs2_client_core = os.path.join(self.config['COMMON']['ORANGEFS_INSTALL_DIR'], "sbin", "pvfs2-client-core")
        for i,client in self.client_hosts.enumerate():
            metadata_server = self.server_meta_hosts[i % len(self.server_meta_hosts)]
            metadata_server_ip = socket.gethostbyname(metadata_server)
            start_client_cmds = [
                "sudo insmod {}".format(kernel_ko),
                "sudo {} -p {}".format(pvfs2_client, pvfs2_client_core),
                "sudo mount -t pvfs2 {protocol}://{ip}:{port}/orangefs {mount_point}".format(
                    protocol=self.config['SERVER']['PVFS2_PROTOCOL'],
                    port=self.config['SERVER']['PVFS2_PORT'],
                    ip=metadata_server_ip,
                    mount_point=self.config['CLIENT']['CLIENT_MOUNT_POINT_DIR'])
            ]
            ExecNode("mount pvfs2 client {}".format(metadata_server),client,start_client_cmds).Run()

    def _DefineStop(self):
        for i, client in self.client_hosts.enumerate():
            cmds = [
                "umount -l {mount_point}".format(mount_point=self.config['CLIENT']['CLIENT_MOUNT_POINT_DIR']),
                "umount -f {mount_point}".format(mount_point=self.config['CLIENT']['CLIENT_MOUNT_POINT_DIR']),
                "umount {mount_point}".format(mount_point=self.config['CLIENT']['CLIENT_MOUNT_POINT_DIR']),
                "killall -9 pvfs2-client",
                "killall -9 pvfs2-client-core",
                "rmmod pvfs2",
                "kill-pvfs2-client"
            ]
            ExecNode("stop client",client, cmds, sudo=True).Run()
        ExecNode("stop server",self.server_data_hosts,"killall -9 pvfs2-server").Run()
        ExecNode("check server", self.client_hosts,"pgrep -la pvfs2-server",print_output=True).Run()

    def _DefineClean(self):
        ExecNode("clean client data", self.client_hosts,
                             "rm -rf {}/*".format(self.config['CLIENT']['CLIENT_MOUNT_POINT_DIR'])).Run()
        ExecNode("clean server data", self.server_data_hosts,
                             "rm -rf {}".format(self.config['SERVER']['SERVER_LOCAL_STORAGE_DIR'])).Run()

    def _DefineStatus(self):
        ExecNode("check clients", self.server_data_hosts, "mount | grep pvfs").Run()
        pvfs2_ping = os.path.join(self.config['COMMON']['ORANGEFS_INSTALL_DIR'], "bin", "pvfs2-ping")
        verify_server_cmd = "export LD_LIBRARY_PATH={pvfs2_lib}; export PVFS2TAB_FILE={client_pvfs2tab}; " \
                            "{pvfs2_ping} -m {mount_point} | grep 'appears to be correctly configured'".format(
            pvfs2_lib=os.path.join(self.config['COMMON']['ORANGEFS_INSTALL_DIR'], "lib"),
            client_pvfs2tab=self.config['CLIENT']['CLIENT_PVFS2TAB_FILE'],
            pvfs2_ping=pvfs2_ping,
            mount_point=self.config['CLIENT']['CLIENT_MOUNT_POINT_DIR']
        )
        ExecNode("check server", self.client_hosts, verify_server_cmd, print_output=True).Run()

