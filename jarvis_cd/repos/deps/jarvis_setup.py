
class JarvisSetup:
    def LocalInstall(self):
        jarvis_root = self.config['jarvis_cd']['path']
        LocalPipNode(jarvis_root, print_fancy=False).Run()

        #Ensure that the variables aren't already being set
        ModifyEnvNode(self.jarvis_env, f"export JARVIS_ROOT", ModifyEnvNodeOps.REMOVE).Run()
        ModifyEnvNode(self.jarvis_env, f"export PYTHONPATH", ModifyEnvNodeOps.REMOVE).Run()

        #Set the variables to their proper values
        ModifyEnvNode(self.jarvis_env, f"export JARVIS_ROOT={jarvis_root}", ModifyEnvNodeOps.APPEND).Run()
        ModifyEnvNode(self.jarvis_env, f"export PYTHONPATH=`sudo -u {self.username} $JARVIS_ROOT/bin/jarvis-py-paths`:$PYTHONPATH", ModifyEnvNodeOps.APPEND).Run()

    def LocalUpdate(self):
        jarvis_root = os.environ['JARVIS_ROOT']
        GitNode(**self.config['jarvis_cd'], method=GitOps.UPDATE, print_fancy=False).Run()
        LocalPipNode(jarvis_root).Run()

    def LocalUninstall(self):
        jarvis_root = os.environ['JARVIS_ROOT']
        shutil.rmtree(jarvis_root)
        ModifyEnvNode(self.jarvis_env, f"export JARVIS_ROOT", ModifyEnvNodeOps.REMOVE).Run()
        ModifyEnvNode(self.jarvis_env, f"export PYTHONPATH", ModifyEnvNodeOps.REMOVE).Run()