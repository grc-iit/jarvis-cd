from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.installer.env_node import EnvNode, EnvNodeOps
from jarvis_cd.installer.installer import Installer
import shutil

class SpackSetup(Installer):
    def LocalInstall(self):
        spack_root = self.config['spack']['path']
        GitNode(**self.config['spack'], method=GitOps.CLONE).Run()
        EnvNode(self.jarvis_env,
                cmd=f"export SPACK_ROOT={spack_root}",
                cmd_re="export SPACK_ROOT",
                op=EnvNodeOps.SET).Run()
        EnvNode(self.jarvis_env,
                cmd=f". {spack_root}/share/spack/setup-env.sh",
                cmd_re=".*setup-env.sh",
                op=EnvNodeOps.SET).Run()

    def LocalUpdate(self):
        GitNode(**self.config['spack'], method=GitOps.UPDATE).Run()

    def LocalUninstall(self):
        spack_root = self.config['spack']['path']
        shutil.rmtree(spack_root)
        EnvNode(self.jarvis_env,
                cmd_re="export SPACK_ROOT",
                op=EnvNodeOps.REMOVE).Run()
        EnvNode(self.jarvis_env,
                cmd_re=f".*setup-env.sh",
                op=EnvNodeOps.REMOVE).Run()