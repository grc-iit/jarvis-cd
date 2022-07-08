from pssh.clients import ParallelSSHClient
from gevent import joinall
import sys, os
import getpass
from jarvis_cd.hostfile import Hostfile

from jarvis_cd.node import Node
from jarvis_cd.exception import Error, ErrorCode

sys.stderr = sys.__stderr__

class SCPNode(Node):
    def __init__(self, name, hosts, sources, destination,
                 username=None, pkey=None, password=None, port=22,
                 sudo=False, print_output=True, collect_output=True, host_aliases=None, ssh_info=None):
        super().__init__(name, print_output, collect_output)

        #Make sure that hosts are a list
        if isinstance(hosts, list):
            self.hosts = hosts
        elif isinstance(hosts, str):
            self.hosts = [hosts]
        elif isinstance(hosts, Hostfile):
            self.hosts = hosts.list()
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SCPNode hosts", type(hosts))

        #Make sure the sources is a list
        if isinstance(sources, list):
            self.sources = sources
        elif isinstance(sources, str):
            self.sources = [sources]
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SCPNode source paths", type(sources))

        #Make sure host_aliases is not None
        if host_aliases is None:
            host_aliases = []

        #Prioritize the SSH_INFO data structure
        if ssh_info is not None:
            if 'username' in ssh_info:
                username = ssh_info['username']
            if 'key' in ssh_info and 'key_dir' in ssh_info:
                pkey = os.path.join(ssh_info['key_dir'], ssh_info['key'])
            if 'port' in ssh_info:
                port = ssh_info['port']
            if 'host_aliases' in ssh_info:
                if isinstance(ssh_info['host_aliases'], list):
                    host_aliases += ssh_info['host_aliases']
                else:
                    host_aliases.append(ssh_info['host_aliases'])
            if 'sudo' in ssh_info:
                sudo = ssh_info['sudo']


        #There's a bug in SCP which cannot copy a file to itself
        for source in self.sources:
            src_file = os.path.normpath(source)
            dst_file = os.path.normpath(destination)
            src_dir = os.path.normpath(os.path.dirname(source))
            dst_dir = os.path.normpath(destination)
            if src_file == dst_file or src_dir == dst_dir:
                self.hosts = self.hosts.copy()
                if 'localhost' in self.hosts:
                    self.hosts.remove('localhost')
                if host_aliases is None:
                    print("WARNING!!! If the machine running this command is also in the hostfile, scp will bug out and remove the data.")
                else:
                    for alias in host_aliases:
                        if alias in self.hosts:
                            self.hosts.remove(alias)
                break

        #Fill in defaults for username, password, and pkey
        if username is None:
            username = getpass.getuser()
        if password is None and pkey is None:
            pkey = f"{os.environ['HOME']}/.ssh/id_rsa"

        self.destination = destination
        self.sudo=sudo
        self.username=username
        self.port = int(port)
        self.pkey = pkey
        self.password = password

    def _Run(self):
        if len(self.hosts) == 0:
            return
        client = ParallelSSHClient(self.hosts, user=self.username, pkey=self.pkey, password=self.password, port=self.port)
        print(self.hosts)
        for source in self.sources:
            destination = self.destination
            if len(self.sources) > 1:
                destination = os.path.join(self.destination, os.path.basename(source))
            output = client.copy_file(source, destination, recurse=os.path.isdir(source))
            joinall(output, raise_error=True)
        return self

    def __str__(self):
        return "SCPNode {}".format(self.name)
