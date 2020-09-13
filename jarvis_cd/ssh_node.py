from pssh.pssh_client import ParallelSSHClient
import sys
import os
import getpass

from jarvis_cd.node import Node

sys.stderr = sys.__stderr__

class SSHNode(Node):
    def __init__(self, name, hosts, cmd, username = getpass.getuser(), sudo=False, print_output=False):
        super().__init__(name, print_output)
        if type(hosts) == list:
            self.hosts=hosts
        else:
            self.hosts=[hosts]
        if type(cmd) == list:
            self.cmds=cmd
        else:
            self.cmds=[cmd]
        self.sudo=sudo
        self.username=username

    def _exec_ssh(self, cmd):
        client = ParallelSSHClient(self.hosts)
        output = client.run_command(cmd, sudo=self.sudo)
        nice_output = dict()
        for host in output:
            nice_output[host]={
                'stdout':[],
                'stderr':[]
            }
            for line in output[host]['stdout']:
                nice_output[host]['stdout'].append(line)
            for line in output[host]['stderr']:
                nice_output[host]['stderr'].append(line)
        return nice_output

    def Run(self):
        outputs = []
        for i,cmd in enumerate(self.cmds):
            output=self._exec_ssh(cmd)
            if self.print_output:
                self.Print(output)
            outputs.append(output)
        return outputs

    def __str__(self):
        return "SSHNode {}".format(self.name)





