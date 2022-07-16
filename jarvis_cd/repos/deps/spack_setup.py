from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.installer.env_node import EnvNode, EnvNodeOps
from jarvis_cd.installer.installer import Installer
import shutil

class SpackSetup(Installer):
    def _LocalInstall(self):
        spack_root = self.config['spack']['path']
        GitNode(**self.config['spack'], method=GitOps.CLONE, collect_output=False, print_fancy=False).Run()
        EnvNode(self.jarvis_env, f"export SPACK_ROOT={spack_root}", "export SPACK_ROOT", EnvNodeOps.SET).Run()
        EnvNode(self.jarvis_env, f". {spack_root}/share/spack/setup-env.sh", "setup-env.sh", EnvNodeOps.SET).Run()

    def _LocalUpdate(self):
        GitNode(**self.config['spack'], method=GitOps.UPDATE, collect_output=False, print_fancy=False).Run()

    def _LocalUninstall(self):
        spack_root = self.config['spack']['path']
        shutil.rmtree(spack_root)
        EnvNode(self.jarvis_env, None, "export SPACK_ROOT", EnvNodeOps.REMOVE).Run()
        EnvNode(self.jarvis_env, None, f".*spack/setup-env.sh", EnvNodeOps.REMOVE).Run()