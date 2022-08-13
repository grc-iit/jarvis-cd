from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.installer.env_node import EnvNode, EnvNodeOps
from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.installer.installer import Installer
import shutil
import os

class ScspkgSetup(Installer):
    def LocalInstall(self):
        scspkg_root = self.config['scspkg']['path']
        GitNode(**self.config['scspkg'], method=GitOps.CLONE).Run()
        cmds = [
            f"chmod +x {scspkg_root}/install.sh",
            f"cd {scspkg_root}",
            f"./install.sh",
        ]
        ExecNode(cmds, shell=True).Run()
        EnvNode(self.jarvis_env,
                cmd=f"export SCSPKG_ROOT={scspkg_root}",
                cmd_re="export SCSPKG_ROOT",
                op=EnvNodeOps.SET).Run()

    def LocalUpdate(self):
        GitNode(**self.config['scspkg'], method=GitOps.UPDATE).Run()

    def LocalUninstall(self):
        scspkg_root = self.config['scspkg']['path']
        shutil.rmtree(scspkg_root)
        EnvNode(self.jarvis_env,
                cmd_re="export SCSPKG_ROOT",
                op=EnvNodeOps.REMOVE).Run()