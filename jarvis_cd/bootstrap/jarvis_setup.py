from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.installer.modify_env_node import ModifyEnvNode, ModifyEnvNodeOps
from jarvis_cd.installer.pip_node import LocalPipNode
from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.bootstrap.package import Package
from jarvis_cd.comm.ssh_node import SSHNode
from jarvis_cd.comm.scp_node import SCPNode
import sys,os
import shutil

class JarvisSetup(Package):
    def Install(self):
        jarvis_root = self.config['jarvis']['path']
        SCPNode('copy jarvis', self.config['jarvis']['path'], self.config['jarvis']['path'], ssh_info=self.ssh_info).Run()
        SSHNode('jarvis deps', f"./{jarvis_root}/dependencies.sh").Run()
        SSHNode('install jarvis', f"cd {self.config['jarvs']['path']}; ./bin/jarvis-bootstrap deps local_install jarvis").Run()

    def _LocalInstall(self):
        jarvis_root = self.config['jarvis']['path']
        GitNode('clone', self.config['jarvis']['repo'], jarvis_root, GitOps.CLONE,
                branch=self.config['jarvis']['branch'], commit=self.config['jarvis']['commit']).Run()
        LocalPipNode('install', jarvis_root).Run()

        #Ensure that the variables aren't already being set
        ModifyEnvNode('jarvis_root', self.bashni, f"export JARVIS_ROOT", ModifyEnvNodeOps.REMOVE).Run()
        ModifyEnvNode('pypath', self.bashni, f"export PYTHONPATH", ModifyEnvNodeOps.REMOVE).Run()

        #Set the variables to their proper values
        ModifyEnvNode('jarvis_root', self.bashni, f"export JARVIS_ROOT={jarvis_root}", ModifyEnvNodeOps.APPEND).Run()
        ModifyEnvNode('pypath', self.bashni, f"export PYTHONPATH=\`sudo -u {self.username} \$JARVIS_ROOT/bin/jarvis-py-paths\`:\$PYTHONPATH", ModifyEnvNodeOps.APPEND).Run()

    def _LocalUpdate(self):
        jarvis_root = os.environ['JARVIS_ROOT']
        GitNode('clone', self.config['jarvis']['repo'], jarvis_root, GitOps.UPDATE,
                branch=self.config['jarvis']['branch'], commit=self.config['jarvis']['commit']).Run()
        ExecNode('deps', f"./{jarvis_root}/dependencies.sh").Run()
        LocalPipNode('install', jarvis_root).Run()

    def _LocalUninstall(self):
        jarvis_root = os.environ['JARVIS_ROOT']
        shutil.rmtree(jarvis_root)
        ModifyEnvNode('jarvis_root', self.bashni, f"export JARVIS_ROOT", ModifyEnvNodeOps.REMOVE).Run()
        ModifyEnvNode('pypath', self.bashni, f"export PYTHONPATH", ModifyEnvNodeOps.REMOVE).Run()