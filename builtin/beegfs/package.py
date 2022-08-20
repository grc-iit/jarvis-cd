from jarvis_cd import *
import os

import re

class EditBeegfsConfig(JarvisExecNode):
    def __init__(self, path, pairs, **kwargs):
        super().__init__(**kwargs)
        self.path = path
        self.pairs = pairs

    def _LocalRun(self):
        with open(self.path, 'r') as fp:
            lines = fp.readlines()
            for i,line in enumerate(lines):
                if re.match('\s+',line):
                    del lines[i]
                for key,value in self.pairs:
                    if key in line and '=' in line:
                        if value is not None:
                            lines[i] = f"{key} = {value}"
                        else:
                            del lines[i]
                        self.pairs.remove((key,value))
            for key,value in self.pairs:
                if value is not None:
                    lines.append(f"{key} = {value}")

        with open(self.path, 'w') as fp:
            fp.write("\n".join(lines))

class Beegfs(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.mgmt_host = self.all_hosts.SelectHosts(self.config['MANAGEMENT_SERVICE']['HOST'])
        self.md_hosts = self.all_hosts.SelectHosts(self.config['METADATA_SERVICE']['HOSTS'])
        self.storage_hosts = self.all_hosts.SelectHosts(self.config['STORAGE_SERVICE']['HOSTS'])
        self.client_hosts = self.all_hosts.SelectHosts(self.config['CLIENT']['HOSTS'])
        self.beegfs_root = self.config['BEEGFS_ROOT']
        self.connauthfile = "/etc/beegfs/connauthfile"

    def _DefineInit(self):
        #Create storage directories
        MkdirNode(self.config['MANAGEMENT_SERVICE']['MOUNT_POINT'], hosts=self.mgmt_host).Run()
        MkdirNode(self.config['METADATA_SERVICE']['MOUNT_POINT'], hosts=self.md_hosts).Run()
        MkdirNode(self.config['STORAGE_SERVICE']['MOUNT_POINT'], hosts=self.storage_hosts).Run()
        MkdirNode(self.config['CLIENT']['MOUNT_POINT'], hosts=self.client_hosts).Run()

        #Prepare storage
        if 'PREPARE_STORAGE' in self.config:
            PrepareStorage(self.config['PREPARE_STORAGE'], hosts=self.storage_hosts).Run()

        #Init mgmt
        cmd = f"{os.path.join(self.beegfs_root, 'sbin', 'beegfs-setup-mgmtd')} -f -p {self.config['MANAGEMENT_SERVICE']['MOUNT_POINT']}"
        ExecNode(cmd, hosts=self.mgmt_host, sudo=True).Run()

        #Init md
        cmd = f"{os.path.join(self.beegfs_root, 'sbin', 'beegfs-setup-meta')} -f -p {self.config['METADATA_SERVICE']['MOUNT_POINT']} -m {self.mgmt_host.ip_str()}"
        ExecNode(cmd, hosts=self.md_hosts, sudo=True).Run()

        #Init storage
        cmd = f"{os.path.join(self.beegfs_root, 'sbin', 'beegfs-setup-storage')} -f -p {self.config['STORAGE_SERVICE']['MOUNT_POINT']} -m {self.mgmt_host.ip_str()}"
        ExecNode(cmd, hosts=self.storage_hosts, sudo=True).Run()

        #Init client
        cmd = f"{os.path.join(self.beegfs_root, 'sbin', 'beegfs-setup-client')} -f -m {self.mgmt_host.ip_str()}"
        ExecNode(cmd, hosts=self.client_hosts, sudo=True).Run()
        cmd = f"echo {self.config['CLIENT']['MOUNT_POINT']} /etc/beegfs/beegfs-client.conf > /etc/beegfs/beegfs-mounts.conf"
        ExecNode(cmd, hosts=self.client_hosts, sudo=True).Run()

        #Create connauth
        ExecNode(f"dd if=/dev/random of={self.connauthfile} bs=128 count=1", sudo=True).Run()
        ChownFS(self.connauthfile, user="root", sudo=True).Run()
        ChmodFS(400, self.connauthfile, sudo=True).Run()
        CopyNode(self.connauthfile, hosts=self.all_hosts, sudo=True).Run()

        #Copy BeeGFS configs
        CopyNode(os.path.join('/etc/beegfs', 'beegfs-mgmtd.conf'),
                 os.path.join(self.per_node_dir, 'beegfs-mgmtd.conf'),
                 hosts=self.mgmt_host).Run()
        CopyNode(os.path.join('/etc/beegfs', 'beegfs-meta.conf'),
                 os.path.join(self.per_node_dir, 'beegfs-meta.conf'),
                 hosts=self.md_hosts).Run()
        CopyNode(os.path.join('/etc/beegfs', 'beegfs-storage.conf'),
                 os.path.join(self.per_node_dir, 'beegfs-storage.conf'),
                 hosts=self.storage_hosts).Run()
        CopyNode(os.path.join('/etc/beegfs', 'beegfs-helperd.conf'),
                 os.path.join(self.per_node_dir, 'beegfs-helperd.conf'),
                 hosts=self.client_hosts).Run()
        CopyNode(os.path.join('/etc/beegfs', 'beegfs-client.conf'),
                 os.path.join(self.per_node_dir, 'beegfs-client.conf'),
                 hosts=self.client_hosts).Run()

        #Edit all configs for connauth
        pairs = [
            ('connAuthFile', self.connauthfile),
            ('storeFsUUID', None)
        ]
        EditBeegfsConfig(os.path.join(self.per_node_dir, 'beegfs-mgmtd.conf'), pairs, hosts=self.mgmt_host).Run()
        EditBeegfsConfig(os.path.join(self.per_node_dir, 'beegfs-meta.conf'), pairs, hosts=self.md_hosts).Run()
        EditBeegfsConfig(os.path.join(self.per_node_dir, 'beegfs-storage.conf'), pairs, hosts=self.storage_hosts).Run()
        EditBeegfsConfig(os.path.join(self.per_node_dir, 'beegfs-helperd.conf'), pairs, hosts=self.storage_hosts).Run()
        EditBeegfsConfig(os.path.join(self.per_node_dir, 'beegfs-client.conf'), pairs, hosts=self.client_hosts).Run()

        # Copy BeeGFS configs back
        CopyNode(os.path.join(self.per_node_dir, 'beegfs-mgmtd.conf'),
                 os.path.join('/etc/beegfs', 'beegfs-mgmtd.conf'),
                 hosts=self.mgmt_host,
                 sudo=True).Run()
        CopyNode(os.path.join(self.per_node_dir, 'beegfs-meta.conf'),
                 os.path.join('/etc/beegfs', 'beegfs-meta.conf'),
                 hosts=self.md_hosts,
                 sudo=True).Run()
        CopyNode(os.path.join(self.per_node_dir, 'beegfs-storage.conf'),
                 os.path.join('/etc/beegfs', 'beegfs-storage.conf'),
                 hosts=self.storage_hosts,
                 sudo=True).Run()
        CopyNode(os.path.join(self.per_node_dir, 'beegfs-helperd.conf'),
                 os.path.join('/etc/beegfs', 'beegfs-helperd.conf'),
                 hosts=self.storage_hosts,
                 sudo=True).Run()
        CopyNode(os.path.join(self.per_node_dir, 'beegfs-client.conf'),
                 os.path.join('/etc/beegfs', 'beegfs-client.conf'),
                 hosts=self.client_hosts,
                 sudo=True).Run()

    def _DefineStart(self):
        ExecNode(f"systemctl start beegfs-mgmtd", hosts=self.mgmt_host, sudo=True).Run()
        ExecNode(f"systemctl start beegfs-meta", hosts=self.md_hosts, sudo=True).Run()
        ExecNode(f"systemctl start beegfs-storage", hosts=self.storage_hosts, sudo=True).Run()
        ExecNode(f"systemctl start beegfs-helperd", hosts=self.client_hosts, sudo=True).Run()
        ExecNode(f"systemctl start beegfs-client", hosts=self.client_hosts, sudo=True).Run()

    def _DefineStop(self):
        ExecNode(f"systemctl stop beegfs-client", hosts=self.client_hosts, sudo=True).Run()
        ExecNode(f"systemctl stop beegfs-helperd", hosts=self.client_hosts, sudo=True).Run()
        ExecNode(f"systemctl stop beegfs-storage", hosts=self.storage_hosts, sudo=True).Run()
        ExecNode(f"systemctl stop beegfs-meta", hosts=self.md_hosts, sudo=True).Run()
        ExecNode(f"systemctl stop beegfs-mgmtd", hosts=self.mgmt_host, sudo=True).Run()
        KillNode('.*beegfs.*', hosts=self.all_hosts).Run()

    def _DefineClean(self):
        if 'PREPARE_STORAGE' in self.config:
            UnprepareStorage(self.config['PREPARE_STORAGE'], hosts=self.storage_hosts).Run()
        RmNode(self.config['MANAGEMENT_SERVICE']['MOUNT_POINT'], hosts=self.mgmt_host, sudo=True).Run()
        RmNode(self.config['METADATA_SERVICE']['MOUNT_POINT'], hosts=self.md_hosts, sudo=True).Run()
        RmNode(self.config['STORAGE_SERVICE']['MOUNT_POINT'], hosts=self.storage_hosts, sudo=True).Run()
        RmNode(self.config['CLIENT']['MOUNT_POINT'], hosts=self.client_hosts, sudo=True).Run()

    def _DefineStatus(self):
        pass