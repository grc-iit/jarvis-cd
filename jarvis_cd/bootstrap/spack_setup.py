from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.installer.modify_env_node import ModifyEnvNode, ModifyEnvNodeOps
from jarvis_cd.bootstrap.package import Package
import sys,os
import shutil

class SpackSetup(Package):
    def _LocalInstall(self):
        # Create SSH directory on all nodes
        spack_root = self.config['spack']['path']
        GitNode(self.config['spack']['repo'], spack_root, GitOps.CLONE,
                branch=self.config['spack']['branch'], commit=self.config['spack']['commit'],
                collect_output=False, print_output=True).Run()
        ModifyEnvNode(self.bashni, f". {spack_root}/share/spack/setup-env.sh", ModifyEnvNodeOps.REMOVE).Run()
        ModifyEnvNode(self.bashni, f". {spack_root}/share/spack/setup-env.sh", ModifyEnvNodeOps.APPEND).Run()

    def _LocalUpdate(self):
        spack_root = os.environ['SPACK_ROOT']
        GitNode(self.config['spack']['repo'], spack_root, GitOps.UPDATE,
                branch=self.config['spack']['branch'], commit=self.config['spack']['commit'],
                collect_output=False, print_output=True).Run()

    def _LocalUninstall(self):
        spack_root = os.environ['SPACK_ROOT']
        shutil.rmtree(spack_root)
        ModifyEnvNode(self.bashni, f". {spack_root}/share/spack/setup-env.sh", ModifyEnvNodeOps.REMOVE).Run()