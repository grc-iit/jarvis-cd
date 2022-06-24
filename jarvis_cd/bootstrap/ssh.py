from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.bootstrap.ssh_args import SSHArgs

class JarvisSSH(SSHArgs):
    def __init__(self, conf):
        self.conf = conf
        self.ParseSSHArgs()

    def Run(self):
        ExecNode('Do SSH', f'ssh -p {self.port} {self.username}@{self.hosts[0]}', collect_output=False).Run()