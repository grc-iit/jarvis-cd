from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.installer.env_node import EnvNode, EnvNodeOps
from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.installer.installer import Installer
import os
import shutil

class SCSRepoSetup(Installer):
    def _LocalInstall(self):
        scs_repo_root = self.config['scs_repo']['path']
        GitNode(**self.config['scs_repo'], method=GitOps.CLONE, collect_output=False, print_output=True, print_fancy=False).Run()
        ExecNode(f'spack repo add {scs_repo_root}').Run()
        EnvNode(self.jarvis_env, f"export SCS_REPO", EnvNodeOps.REMOVE).Run()
        EnvNode(self.jarvis_env, f"export SCS_REPO={scs_repo_root}", EnvNodeOps.APPEND).Run()

    def _LocalUpdate(self):
        scs_repo_root = os.environ['SCS_REPO']
        GitNode(**self.config['scs_repo'], method=GitOps.UPDATE, collect_output=False, print_output=True, print_fancy=False).Run()

    def _LocalUninstall(self):
        scs_repo_root = os.environ['SCS_REPO']
        shutil.rmtree(scs_repo_root)
        EnvNode(self.jarvis_env, f"export SCS_REPO", EnvNodeOps.REMOVE).Run()