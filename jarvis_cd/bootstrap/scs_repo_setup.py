import argparse
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.comm.ssh_node import SSHNode
from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.bootstrap.ssh_args import SSHArgs
from jarvis_cd.bootstrap.git_args import GitArgs
from jarvis_cd.basic.check_command import CheckCommandNode
import sys,os

class SCSRepoSetup(SSHArgs,GitArgs):
    def __init__(self, conf, operation):
        self.conf = conf
        self.ParseSSHArgs()
        self.ParseGitArgs('scs_repo')
        self.operation = operation

    def Run(self):
        if self.operation == 'install':
            self.Install()
        elif self.operation == 'update':
            self.Update()
        elif self.operation == 'uninstall':
            self.Uninstall()

    def Install(self):
        # Create SSH directory on all nodes
        cmds = []
        self.GitCloneCommands(cmds)
        cmds.append(f'spack repo add ../scs-repo')
        cmds.append(f'echo export SCS_REPO=$PWD >> ~/.bashni')
        SSHNode('Install SCS repo', self.hosts, cmds, pkey=self.private_key, username=self.username, port=self.port, collect_output=False, do_ssh=self.do_ssh).Run()

    def Update(self):
        cmds = []
        self.GitUpdateCommands(cmds, '$SCS_REPO')
        SSHNode('Update SCS repo', self.hosts, cmds, pkey=self.private_key, username=self.username, port=self.port, collect_output=False, do_ssh=self.do_ssh).Run()

    def Uninstall(self):
        cmds = [
            f'python3 $JARVIS_ROOT/bin/jarvis-bootstrap spack reset_bashrc',
            f'rm -rf $SCS_REPO'
        ]
        SSHNode('Uninstall scs-repo', self.hosts, cmds, pkey=self.private_key, username=self.username, port=self.port, collect_output=False, do_ssh=self.do_ssh).Run()