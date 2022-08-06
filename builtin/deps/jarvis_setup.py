
from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.installer.pip_node import LocalPipNode
from jarvis_cd.installer.env_node import EnvNode, EnvNodeOps
from jarvis_cd.installer.installer import Installer
from jarvis_cd.fs.mkdir_node import MkdirNode
from jarvis_cd.fs.fs import ChownFS
import os
import shutil

class JarvisSetup(Installer):
    def LocalInstall(self):
        #Set the variables to their proper values
        EnvNode(self.jarvis_env,
                cmd=f"export JARVIS_ROOT={self.jarvis_root}",
                cmd_re="export JARVIS_ROOT",
                op=EnvNodeOps.SET).Run()
        EnvNode(self.jarvis_env,
                cmd=f"export JARVIS_SHARED_PKG_DIR={self.jarvis_shared_pkg_dir}",
                cmd_re="export JARVIS_SHARED_PKG_DIR",
                op=EnvNodeOps.SET).Run()
        EnvNode(self.jarvis_env,
                cmd=f"export JARVIS_PER_NODE_PKG_DIR={self.jarvis_per_node_pkg_dir}",
                cmd_re="export JARVIS_PER_NODE_PKG_DIR",
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