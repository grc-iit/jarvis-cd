from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.installer.env_node import EnvNode, EnvNodeOps
from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.installer.installer import Installer
import os
import shutil


class ScsRepoSetup(Installer):
    def LocalInstall(self):
        scs_repo_root = self.config['scs_repo']['path']
        GitNode(**self.config['scs_repo'], method=GitOps.CLONE).Run()
        ExecNode(f'spack repo add {scs_repo_root}').Run()
        EnvNode(self.jarvis_env,
                cmd=f"export SCS_REPO={scs_repo_root}",
                cmd_re=f"export SCS_REPO",
                op=EnvNodeOps.SET).Run()

    def LocalUpdate(self):
        scs_repo_root = os.environ['SCS_REPO']
        GitNode(**self.config['scs_repo'], method=GitOps.UPDATE).Run()

    def LocalUninstall(self):
        scs_repo_root = os.environ['SCS_REPO']
        shutil.rmtree(scs_repo_root)
        EnvNode(self.jarvis_env,
                cmd_re=f"export SCS_REPO",
                op=EnvNodeOps.REMOVE).Run()