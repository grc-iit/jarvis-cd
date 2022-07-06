from pssh.clients import ParallelSSHClient
import sys
import os
import getpass
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.basic.exec_node import ExecNode

from jarvis_cd.node import Node
from jarvis_cd.exception import Error, ErrorCode

sys.stderr = sys.__stderr__

class SSHNode(Node):
    def __init__(self, name, hosts, cmds,
                 username=None, pkey=None, password=None, port=22,
                 sudo=False, print_output=True, collect_output=True, do_ssh=True):
        super().__init__(name, print_output, collect_output)

        if username is None:
            username = getpass.getuser()
        if isinstance(hosts, list):
            self.hosts=hosts
        elif isinstance(hosts, str):
            self.hosts=[hosts]
        elif isinstance(hosts, Hostfile):
            self.hosts = hosts.list()
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SSHNode hosts", type(hosts))

        if isinstance(cmds, list):
            self.cmds=cmds
        elif isinstance(cmds, str):
            self.cmds=[cmds]
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SSHNode cmds", type(cmds))

        if password is None and pkey is None:
            pkey = f"{os.environ['HOME']}/.ssh/id_rsa"

        #Do not execute SSH if only localhost
        if self.hosts[0] == 'localhost' and len(self.hosts) == 1:
            do_ssh = False

        self.pkey = pkey
        self.password = password
        self.sudo=sudo
        self.username=username
        self.port = int(port)
        self.do_ssh = do_ssh

    def _exec_ssh(self, cmd):
        client = ParallelSSHClient(self.hosts, user=self.username, pkey=self.pkey, password=self.password, port=self.port)
        output = client.run_command(cmd, sudo=self.sudo)
        nice_output = dict()
        for host_output in output:
            host = host_output.host
            nice_output[host]={
                'stdout':[],
                'stderr':[]
            }
            nice_output[host]['stdout'] = list(host_output.stdout)
            nice_output[host]['stderr'] = list(host_output.stderr)
        return nice_output

    def _Run(self):
        #self.output = [self._exec_ssh(cmd) for i,cmd in enumerate(self.cmds)]
        if self.do_ssh:
            cmd = " ; ".join(self.cmds)
            self.output = self._exec_ssh(cmd)
        else:
            self.output = ExecNode('SSH Command', self.cmds, self.print_output, self.collect_output, shell=True).Run().GetOutput()
        return self

    def __str__(self):
        return "SSHNode {}".format(self.name)
