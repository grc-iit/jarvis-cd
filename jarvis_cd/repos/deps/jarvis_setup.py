
from jarvis_cd.installer.installer import Installer

class JarvisSetup(Installer):
    def LocalInstall(self):
        jarvis_root = self.config['jarvis_cd']['path']
        LocalPipNode(jarvis_root, print_fancy=False).Run()

        #Set the variables to their proper values
        EnvNode(self.jarvis_env, "JARVIS_ROOT", EnvNodeOps.SET).Run()
        EnvNode(self.jarvis_env, "PYTHONPATH", "`sudo -u {self.username} $JARVIS_ROOT/bin/jarvis-py-paths`:$PYTHONPATH", EnvNodeOps.SET).Run()

    def LocalUpdate(self):
        jarvis_root = os.environ['JARVIS_ROOT']
        GitNode(**self.config['jarvis_cd'], method=GitOps.UPDATE, print_fancy=False).Run()
        LocalPipNode(jarvis_root).Run()

    def LocalUninstall(self):
        jarvis_root = os.environ['JARVIS_ROOT']
        shutil.rmtree(jarvis_root)
        EnvNode(self.jarvis_env, f"export JARVIS_ROOT", EnvNodeOps.REMOVE).Run()
        EnvNode(self.jarvis_env, f"export PYTHONPATH", EnvNodeOps.REMOVE).Run()