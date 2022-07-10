
from jarvis_cd.basic.exec_node import ExecNode
from abc import ABC, abstractmethod
from jarvis_cd.comm.ssh_config import SSHConfig
from enum import Enum
import os

class BootstrapConfig(SSHConfig):
    def DefaultConfigPath(self, conf_type='remote'):
        return os.path.join(self.jarvis_root, 'jarvis_cd', 'bootstrap', 'conf', f'{conf_type}.yaml')

    def _ProcessConfig(self):
        super()._ProcessConfig()

        self.key_name = 'id_rsa'
        self.key_dir = os.path.join(os.environ["HOME"], ".ssh")
        self.dst_key_dir = self.key_dir
        self.ssh_keys = {}

        if "ssh_keys" in self.config:
            self.ssh_keys = self.config["ssh_keys"]
        if 'SSH' in self.config:
            self.ssh_keys['primary'] = self.config['SSH']
        self.key_dir, self.key_name, self.dst_key_dir = self._GetKeyInfo('primary')

    def _GetKeyInfo(self, key_entry):
        key_dir = os.path.join(os.environ["HOME"], ".ssh")
        key_name = 'id_rsa'
        dst_key_dir = os.path.join("home", self.username, ".ssh")
        if key_entry in self.ssh_keys:
            key_dir = self.ssh_keys[key_entry]["key_dir"]
            key_name = self.ssh_keys[key_entry]["key"]
            if 'dst_key_dir' in self.ssh_keys[key_entry]:
                dst_key_dir = self.ssh_keys[key_entry]["dst_key_dir"]
        return key_dir,key_name,dst_key_dir

class Package(BootstrapConfig):
    def __init__(self, operation, package_name):
        super().__init__()
        self.operation = operation
        self.bashrc = f"{os.environ['HOME']}/.bashrc"
        self.bashni = f"{os.environ['HOME']}/.bashni"
        self.package_name = package_name

    def Run(self):
        if self.operation == 'install':
            self.Install()
        elif self.operation == 'update':
            self.Update()
        elif self.operation == 'uninstall':
            self.Uninstall()
        elif self.operation == 'local_install':
            self._LocalInstall()
        elif self.operation == 'local_update':
            self._LocalUpdate()
        elif self.operation == 'local_uninstall':
            self._LocalUninstall()

    def Install(self):
        ExecNode(f"jarvis-bootstrap deps local_install {self.package_name}", hosts=self.all_hosts, ssh_info=self.ssh_info).Run()

    def Update(self):
        ExecNode(f"jarvis-bootstrap deps local_update {self.package_name}", hosts=self.all_hosts, ssh_info=self.ssh_info).Run()

    def Uninstall(self):
        ExecNode(f"jarvis-bootstrap deps local_uninstall {self.package_name}", hosts=self.all_hosts, ssh_info=self.ssh_info).Run()

    @abstractmethod
    def _LocalInstall(self):
        pass

    @abstractmethod
    def _LocalUpdate(self):
        pass

    @abstractmethod
    def _LocalUninstall(self):
        pass