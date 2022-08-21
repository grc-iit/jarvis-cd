import os
from jarvis_cd.basic.exception import Error, ErrorCode
import socket
import re

class Hostfile:
    def __init__(self, hosts):
        self.hosts_ip = None
        self.hosts = None
        self.path = None
        if hosts is None:
            hosts = []
        if isinstance(hosts, str):
            self._load_hostfile(hosts)
        else:
            self._set_hosts(hosts)

    def _load_hostfile(self, path):
        if not os.path.exists(path):
            raise Error(ErrorCode.HOSTFILE_NOT_FOUND).format(path)
        hosts = []
        with open(path, 'r') as fp:
            lines = fp.read().splitlines()
            for line in lines:
                tokens = line.split('#')
                host = tokens[0].strip()
                if len(host) == 0:
                    continue
                hosts.append(host)
        self.path = path
        self._set_hosts(hosts)
        return self

    def _set_hosts(self, hosts):
        self.hosts = hosts
        self.hosts_ip = [socket.gethostbyname(host) for host in hosts]
        return self

    #Hostset: 1,5-8,10
    def SelectHosts(self, hostset):
        #Hosts are numbered from 1
        hosts = []
        hostset = str(hostset)
        ranges = hostset.split(',')
        for range in ranges:
            range = range.split('-')
            if len(range) == 2:
                min = int(range[0])
                max = int(range[1])
                if min > max or min < 1 or max > len(self.hosts):
                    raise Error(ErrorCode.INVALID_HOST_RANGE).format(len(self.hosts), min, max)
                hosts += self.hosts[min-1:max]
            else:
                val = int(range[0])
                if val < 1 or val > len(self.hosts):
                    raise Error(ErrorCode.INVALID_HOST_ID).format(len(self.hosts), val)
                hosts += [self.hosts[val-1]]
        return Hostfile(hosts)

    def Path(self):
        return self.path

    def ip_list(self):
        return self.hosts_ip

    def hostname_list(self):
        return self.hosts

    def enumerate(self):
        return enumerate(self.hosts)

    def host_str(self, sep=','):
        return sep.join(self.hosts)

    def ip_str(self, sep=','):
        return sep.join(self.hosts_ip)

    def __len__(self):
        return len(self.hosts)

    def __getitem__(self, idx):
        return self.hosts[idx]

    def __str__(self):
        return str(self.hosts)

    def __repr__(self):
        return str(self)
