from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.installer.modify_env_node import ModifyEnvNode, ModifyEnvNodeOps
from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.bootstrap.package import Package
import sys,os
import shutil

class SCSRepoSetup(Package):
    def _LocalInstall(self):
        scs_repo_root = self.config['scs_repo']['path']
        GitNode(self.config['scs_repo']['repo'], scs_repo_root, GitOps.CLONE,
                branch=self.config['scs_repo']['branch'], commit=self.config['scs_repo']['commit'],
                collect_output=False, print_output=True).Run()
        ExecNode(f'spack repo add {scs_repo_root}').Run()
        ModifyEnvNode(self.jarvis_env, f"export SCS_REPO", ModifyEnvNodeOps.REMOVE).Run()
        ModifyEnvNode(self.jarvis_env, f"export SCS_REPO={scs_repo_root}", ModifyEnvNodeOps.APPEND).Run()

    def _LocalUpdate(self):
        scs_repo_root = os.environ['SCS_REPO']
        GitNode(self.config['scs_repo']['repo'], scs_repo_root, GitOps.UPDATE,
                branch=self.config['scs_repo']['branch'], commit=self.config['scs_repo']['commit'],
                collect_output=False, print_output=True).Run()

    def _LocalUninstall(self):
        scs_repo_root = os.environ['SCS_REPO']
        shutil.rmtree(scs_repo_root)
        ModifyEnvNode(self.jarvis_env, f"export SCS_REPO", ModifyEnvNodeOps.REMOVE).Run()