from jarvis_cd.exception import Error, ErrorCode
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.node import Node
from jarvis_cd.ssh.ssh_info_mixin import SSHInfoMixin
import sys,os


class ParallelNode(Node,SSHInfoMixin):
    def __init__(self, hosts=None, affinity=None, sleep_period_ms=100, max_retries=0, cwd=None, shell=False, sudo=False,
                 exec_async=False, **kwargs):
        super().__init__(**kwargs)

        # Make sure hosts in proper format
        if hosts is None:
            hosts = []
        if isinstance(hosts, list):
            self.hosts = hosts
        elif isinstance(hosts, str):
            self.hosts = [hosts]
        elif isinstance(hosts, Hostfile):
            self.hosts = hosts.list()
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SSHExecNode hosts", type(hosts))

        self._ProcessSSHInfo()
        self.sudo = sudo
        self.shell = shell
        self.affinity = affinity
        self.sleep_period_ms = sleep_period_ms
        self.max_retries = max_retries
        self.exec_async = exec_async
        self.cwd = cwd

        #Do SSH only if the host list contains more than host aliases

