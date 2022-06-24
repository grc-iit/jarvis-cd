import argparse
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.comm.ssh_node import SSHNode
from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.bootstrap.ssh_args import SSHArgs
from jarvis_cd.bootstrap.git_args import GitArgs
import sys,os

class JarvisSetup(SSHArgs,GitArgs):
    def __init__(self, conf, operation):
        self.conf = conf
        self.ParseSSHArgs()
        self.ParseGitArgs('jarvis_cd')
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
        cmds = []
        self.GitCloneCommands(cmds)
        cmds.append(f'bash dependencies.sh')
        cmds.append(f'echo \"export JARVIS_ROOT=$HOME/jarvis-cd\" >> ~/.bashni')
        cmds.append(f'python3 -m pip install -r requirements.txt')
        cmds.append(f'python3 -m pip install -e . --user')
        SSHNode('Install Jarvis', self.hosts, cmds, pkey=self.private_key, username=self.username, port=self.port,
                collect_output=False, do_ssh=self.do_ssh).Run()

    def Update(self):
        cmds = []
        self.GitUpdateCommands(cmds, '$JARVIS_ROOT')
        cmds.append(f'python3 -m pip install -r requirements.txt')
        SSHNode('Update jarvis', self.hosts, cmds, pkey=self.private_key, username=self.username, port=self.port,
                collect_output=False, do_ssh=self.do_ssh).Run()

    def Uninstall(self):
        cmds = []
        cmds.append(f'$JARVIS_ROOT/bin/jarvis-bootstrap jarvis reset_bashrc')
        cmds.append(f'rm -rf $JARVIS_ROOT')
        SSHNode('Uninstall Jarvis', self.hosts, cmds, pkey=self.private_key, username=self.username, port=self.port,
                collect_output=False, do_ssh=self.do_ssh).Run()

    def ResetBashrc(self):
        with open(f'{os.environ["HOME"]}/.bashni', 'r') as fp:
            bashrc = fp.read()
            bashrc = bashrc.replace(f'export JARVIS_ROOT={os.environ["HOME"]}/jarvis-cd\n', '')
        with open(f'{os.environ["HOME"]}/.bashni', 'w') as fp:
            fp.write(bashrc)