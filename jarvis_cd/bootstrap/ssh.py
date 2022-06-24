from jarvis_cd.basic.exec_node import ExecNode

class JarvisSSH(SSHArgs):
    def __init__(self, conf):
        self.conf = conf
        self.ParseSSHArgs()

    def Run(self):
        ExecNode('Do SSH', f'ssh -p {self.port} {self.username}@{host}', collect_output=False).Run()