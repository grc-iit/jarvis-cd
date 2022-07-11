from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.installer.modify_env_node import ModifyEnvNode, ModifyEnvNodeOps
from jarvis_cd.bootstrap.package import Package
import sys,os
import shutil

class SpackSetup(Package):
    def _LocalInstall(self):
        spack_root = self.config['spack']['path']
        GitNode(**self.config['spack'], method=GitOps.CLONE, collect_output=False).Run()
        ModifyEnvNode(self.jarvis_env, f"spack/setup-env.sh", ModifyEnvNodeOps.REMOVE).Run()
        ModifyEnvNode(self.jarvis_env, f". {spack_root}/share/spack/setup-env.sh", ModifyEnvNodeOps.APPEND).Run()

    def _LocalUpdate(self):
        spack_root = os.environ['SPACK_ROOT']
        GitNode(**self.config['spack'], method=GitOps.UPDATE, collect_output=False).Run()

    def _LocalUninstall(self):
        spack_root = os.environ['SPACK_ROOT']
        shutil.rmtree(spack_root)
        ModifyEnvNode(self.jarvis_env, f". {spack_root}/share/spack/setup-env.sh", ModifyEnvNodeOps.REMOVE).Run()