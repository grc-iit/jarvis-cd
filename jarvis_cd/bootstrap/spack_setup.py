import argparse
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.comm.ssh_node import SSHNode
from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.bootstrap.ssh_args import SSHArgs
from jarvis_cd.bootstrap.git_args import GitArgs
from jarvis_cd.basic.check_command import CheckCommandNode
import sys,os

class SpackSetup(SSHArgs,GitArgs):
    def __init__(self, conf, operation):
        self.conf = conf
        self.ParseSSHArgs()
        self.ParseGitArgs('spack')
        self.operation = operation

    def Run(self):
        if self.operation == 'install':
            self.Install()
        elif self.operation == 'update':
            self.Update()
        elif self.operation == 'uninstall':
            self.Uninstall()
        elif self.operation == 'reset_bashrc':
            self.ResetBashrc()

    def Install(self):
        # Create SSH directory on all nodes
        cmds = []
        if CheckCommandNode('Check Spack', 'spack').Run().Exists():
            print("Spack already exists")
            return
        self.GitCloneCommand(cmds)
        cmds.append(f'echo ". $HOME/spack/share/spack/setup-env.sh" >> ~/.bashni')
        SSHNode('Install spack', self.hosts, cmds, pkey=self.private_key, username=self.username, port=self.port, collect_output=False, do_ssh=self.do_ssh).Run()

    def Update(self):
        cmds = []
        self.GitUpdateCommand(cmds, '$SPACK_ROOT')
        SSHNode('Update spack', self.hosts, cmds, pkey=self.private_key, username=self.username, port=self.port, collect_output=False, do_ssh=self.do_ssh).Run()

    def Uninstall(self):
        cmds = [
            f'python3 $JARVIS_ROOT/bin/jarvis-bootstrap spack reset_bashrc',
            f'rm -rf $SPACK_ROOT'
        ]
        SSHNode('Uninstall spack', self.hosts, cmds, pkey=self.private_key, username=self.username, port=self.port, collect_output=False, do_ssh=self.do_ssh).Run()

    def ResetBashrc(self):
        with open(f'{os.environ["HOME"]}/.bashni', 'r') as fp:
            bashrc = fp.read()
            bashrc = bashrc.replace(f'. {os.environ["HOME"]}/spack/share/spack/setup-env.sh\n', '')
        with open(f'{os.environ["HOME"]}/.bashni', 'w') as fp:
            fp.write(bashrc)