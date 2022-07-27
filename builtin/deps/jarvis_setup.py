
from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.installer.pip_node import LocalPipNode
from jarvis_cd.installer.env_node import EnvNode, EnvNodeOps
from jarvis_cd.installer.installer import Installer
import os
import shutil

class JarvisSetup(Installer):
    def LocalInstall(self):
        jarvis_root = self.config['jarvis_cd']['path']
        jarvis_shared = self.config['JARVIS_SHARED']

        #Set the variables to their proper values
        EnvNode(self.jarvis_env,
                cmd=f"export JARVIS_ROOT={jarvis_root}",
                cmd_re="export JARVIS_ROOT",
                op=EnvNodeOps.SET).Run()
        EnvNode(self.jarvis_env,
                cmd=f"export JARVIS_SHARED={int(jarvis_shared)}",
                cmd_re="export JARVIS_SHARED",
                op=EnvNodeOps.SET).Run()
        EnvNode(self.jarvis_env,
                "export PYTHONPATH=`sudo -u {self.username} $JARVIS_ROOT/bin/jarvis-py-paths`:$PYTHONPATH",
                "export PYTHONPATH",
                EnvNodeOps.SET).Run()

    def LocalUpdate(self):
        jarvis_root = os.environ['JARVIS_ROOT']
        GitNode(**self.config['jarvis_cd'], method=GitOps.UPDATE).Run()
        LocalPipNode(jarvis_root).Run()

    def LocalUninstall(self):
        jarvis_root = os.environ['JARVIS_ROOT']
        shutil.rmtree(jarvis_root)