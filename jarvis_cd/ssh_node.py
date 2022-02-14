from pssh.clients import ParallelSSHClient
import sys
import os
import getpass
from jarvis_cd.hostfile import Hostfile

from jarvis_cd.node import Node
from jarvis_cd.exception import Error, ErrorCode

sys.stderr = sys.__stderr__

class SSHNode(Node):
    def __init__(self, name, hosts, cmd, username = getpass.getuser(), port=22, sudo=False, print_output=False):
        super().__init__(name, print_output)
        if isinstance(hosts, list):
            self.hosts=hosts
        elif isinstance(hosts, str):
            self.hosts=[hosts]
        elif isinstance(hosts, Hostfile):
            self.hosts = hosts.list()
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SSHNode hosts", type(hosts))

        if isinstance(cmd, list):
            self.cmds=cmd
        elif isinstance(cmd, str):
            self.cmds=[cmd]
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SSHNode cmdss", type(cmd))

        self.sudo=sudo
        self.username=username
        self.port = port

    def _exec_ssh(self, cmd):
        client = ParallelSSHClient(self.hosts, port=self.port)
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
