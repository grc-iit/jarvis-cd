
class ParallelDeps:
    def __init__(self, conf):
        self.conf = conf

    def Install(self):
        #Install dependencies in parallel
        SCPNode('Copy Jarvis', self.hosts, )
        SSHNode('Install Dependencies', self.hosts, cmds, pkey=priv_key, username=self.username, port=self.port,
                collect_output=False).Run()
        return

    def Update(self):
        return

    def Uninstall(self):
        return

    def ResetBashrc(self):
        return