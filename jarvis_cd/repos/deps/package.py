from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.installer.modify_env_node import ModifyEnvNode, ModifyEnvNodeOps
from jarvis_cd.installer.pip_node import LocalPipNode
from jarvis_cd.bootstrap.package import Package
from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.shell.copy_node import CopyNode
import os
import shutil

class Deps(Application):
    def __init__(self):
        super().__init__()
        self.bashrc = f"{os.environ['HOME']}/.bashrc"
        self.jarvis_env = f"{os.environ['HOME']}/.bashni"

    def _Scaffold(self):
        #Check if jarvis already installed
        if 'JARVIS_ROOT' in os.environ:
            self.config['jarvis_cd']['path'] = os.environ['JARVIS_ROOT']

        #Check if spack already installed
        node = CheckCommandNode('spack').Run()
        if node.Exists():
            self.config['spack']['path'] = os.environ['SPACK_ROOT']

        #Check if scsrepo already installed
        if 'SCS_REPO' in os.environ:
            self.config['scs_repo']['path'] = os.environ['SCS_REPO']

    def Install(self, package_name):
        if package_name == 'jarvis' or package_name == 'all':
            jarvis_root = self.config['jarvis_cd']['path']
            jarvis_conf = os.path.join(jarvis_root, 'jarvis_conf.yaml')
            GitNode(**self.config['jarvis_cd'], method=GitOps.CLONE, hosts=self.all_hosts, ssh_info=self.ssh_info).Run()
            CopyNode(jarvis_conf, jarvis_conf, hosts=self.all_hosts, ssh_info=self.ssh_info).Run()
            cmds = [
                f"chmod +x {jarvis_root}/dependencies.sh",
                f"{jarvis_root}/dependencies.sh",
                f"export PYTHONPATH={jarvis_root}",
                f"cd {self.config['jarvis_cd']['path']}",
                f"./bin/jarvis-bootstrap deps local_install {package_name}"
            ]
            ExecNode(cmds, hosts=self.all_hosts, ssh_info=self.ssh_info).Run()
        else:
            ExecNode(f"jarvis deps local-install {self.package_name}", hosts=self.all_hosts, ssh_info=self.ssh_info).Run()

    def Update(self, package_name):
        ExecNode(f"jarvis deps local-update {package_name}", hosts=self.all_hosts, ssh_info=self.ssh_info).Run()

    def Uninstall(self, package_name):
        ExecNode(f"jarvis deps local-uninstall {package_name}", hosts=self.all_hosts, ssh_info=self.ssh_info).Run()

    def LocalInstall(self, package_name):
        return

    def LocalUpdate(self, package_name):
        return

    def LocalUninstall(self, package_name):
        return