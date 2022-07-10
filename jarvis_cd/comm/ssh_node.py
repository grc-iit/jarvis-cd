from pssh.clients import ParallelSSHClient
import sys
import os
import getpass
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.comm.ssh_config import SSHArgs

from jarvis_cd.node import Node
from jarvis_cd.exception import Error, ErrorCode

sys.stderr = sys.__stderr__

class SSHNode(Node,SSHArgs):
    def __init__(self, name, cmds,
                 hosts=None, username=None, pkey=None, password=None, port=22,
                 sudo=False, shell=True, host_aliases=None, ssh_info=None,
                 exec_async=False, print_output=True, collect_output=True):
        super().__init__(name, print_output, collect_output)
        self._ProcessArgs(hosts=hosts, username=username, pkey=pkey, password=password, port=port,
                          sudo=sudo, shell=shell, host_aliases=host_aliases, ssh_info=ssh_info)

        #Make sure commands are a list
        if isinstance(cmds, list):
            self.cmds=cmds
        elif isinstance(cmds, str):
            self.cmds=[cmds]
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SSHNode cmds", type(cmds))

        self.exec_async = exec_async

    def _exec_ssh(self, cmd):
        client = ParallelSSHClient(self.hosts, user=self.username, pkey=self.pkey, password=self.password, port=self.port)
        output = client.run_command(cmd, sudo=self.sudo, use_pty=not self.exec_async)
        nice_output = dict()
        for host_output in output:
            host = host_output.host
            nice_output[host]={
                'stdout':[],
                'stderr':[]
            }
            nice_output[host]['stdout'] = list(host_output.stdout)
            nice_output[host]['stderr'] = list(host_output.stderr)
            nice_output[host]['stdout'].insert(0, cmd)
        return [nice_output]

    def _Run(self):
        if self.sudo:
            self.cmds.insert(0, f"source /home/{self.username}/.bashrc")
        if self.do_ssh:
            cmd = " ; ".join(self.cmds)
            self.output = self._exec_ssh(cmd)
        else:
            self.output = ExecNode('SSH Command', self.cmds, self.print_output, self.collect_output, shell=self.shell, sudo=self.sudo, exec_async=self.exec_async).Run().GetOutput()
        return self

    def __str__(self):
        return "SSHNode {}".format(self.name)
