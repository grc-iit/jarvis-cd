from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.bootstrap.ssh_args import SSHArgs

class JarvisSSH(SSHArgs):
    def __init__(self, conf, host_id=0):
        self.conf = conf
        self.host_id = host_id
        self.ParseSSHArgs()

    def Run(self):
        ExecNode('Do SSH', f'ssh -p {self.port} {self.username}@{self.hosts[self.host_id]}', collect_output=False).Run()