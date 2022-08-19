from jarvis_cd import *
import os

class Beegfs(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.mgmt_host = self.all_hosts.SelectHosts(self.config['MANAGEMENT_SERVICE']['HOST'])
        self.md_hosts = self.all_hosts.SelectHosts(self.config['METADATA_SERVICE']['HOSTS'])
        self.storage_hosts = self.all_hosts.SelectHosts(self.config['STORAGE_SERVICE']['HOSTS'])
        self.client_hosts = self.all_hosts.SelectHosts(self.config['CLIENT']['HOSTS'])
        self.beegfs_root = self.config['BEEGFS_ROOT']

    def _DefineInit(self):
        #Create storage directories
        MkdirNode(self.config['MANAGEMENT_SERVICE']['MOUNT_POINT'], hosts=self.mgmt_hosts).Run()
        MkdirNode(self.config['METADATA_SERVICE']['MOUNT_POINT'], hosts=self.md_hosts).Run()
        MkdirNode(self.config['STORAGE_SERVICE']['MOUNT_POINT'], hosts=self.storage_hosts).Run()
        MkdirNode(self.config['CLIENT']['MOUNT_POINT'], hosts=self.client_hosts).Run()

        #Prepare storage
        if 'PREPARE_STORAGE' in self.config:
            PrepareStorage(self.config['PREPARE_STORAGE'], hosts=self.server_hosts).Run()

        #Init mgmt
        cmd = f"{os.path.join(self.beegfs_root, 'sbin', 'beegfs-setup-mgmtd')} -p {self.config['MANAGEMENT_SERVICE']['MOUNT_POINT']}"
        ExecNode(cmd, hosts=self.mgmt_host, sudo=True).Run()

        #Init md
        cmd = f"{os.path.join(self.beegfs_root, 'sbin', 'beegfs-setup-meta')} -p {self.config['METADATA_SERVICE']['MOUNT_POINT']} -m {self.mgmt_host}"
        ExecNode(cmd, hosts=self.md_hosts, sudo=True).Run()

        #Init storage
        cmd = f"{os.path.join(self.beegfs_root, 'sbin', 'beegfs-setup-storage')} -p {self.config['STORAGE_SERVICE']['MOUNT_POINT']} -m {self.mgmt_host}"
        ExecNode(cmd, hosts=self.storage_hosts, sudo=True).Run()

        #Init client
        cmd = f"{os.path.join(self.beegfs_root, 'sbin', 'beegfs-setup-client')} -m {self.client_hosts}"
        ExecNode(cmd, hosts=self.client_hosts, sudo=True).Run()
        cmd = f"echo {self.config['CLIENT']['MOUNT_POINT']} /etc/beegfs/beegfs-client.conf > /etc/beegfs/beegfs-mounts.conf"
        ExecNode(cmd, hosts=self.client_hosts).Run()

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

    def _DefineClean(self):
        if 'PREPARE_STORAGE' in self.config:
            UnprepareStorage(self.config['PREPARE_STORAGE'], hosts=self.server_hosts).Run()
        RmNode(self.config['MANAGEMENT_SERVICE']['MOUNT_POINT'], hosts=self.mgmt_hosts).Run()
        RmNode(self.config['METADATA_SERVICE']['MOUNT_POINT'], hosts=self.md_hosts).Run()
        RmNode(self.config['STORAGE_SERVICE']['MOUNT_POINT'], hosts=self.storage_hosts).Run()
        RmNode(self.config['CLIENT']['MOUNT_POINT'], hosts=self.client_hosts).Run()

    def _DefineStatus(self):
        pass