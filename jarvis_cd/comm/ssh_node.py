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
                 sudo=False, print_output=True, collect_output=True, do_ssh=True, exec_async=False, shell=True,
                 ssh_info=None):
        super().__init__(name, print_output, collect_output)

        #Make sure hosts in proper format
        if isinstance(hosts, list):
            self.hosts=hosts
        elif isinstance(hosts, str):
            self.hosts=[hosts]
        elif isinstance(hosts, Hostfile):
            self.hosts = hosts.list()
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SSHNode hosts", type(hosts))

        #Make sure username is set
        if username is None:
            username = getpass.getuser()

        #Prioritize ssh_info structure
        if ssh_info is not None:
            if 'username' in ssh_info:
                username = ssh_info['username']
            if 'key' in ssh_info and 'key_dir' in ssh_info:
                pkey = os.path.join(ssh_info['key_dir'], ssh_info['key'])
            if 'port' in ssh_info:
                port = ssh_info['port']
            if 'sudo' in ssh_info:
                sudo = ssh_info['sudo']
            if 'shell' in ssh_info:
                shell = ssh_info['shell']

        #Make sure commands is a list
        if isinstance(cmds, list):
            self.cmds=cmds
        elif isinstance(cmds, str):
            self.cmds=[cmds]
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SSHNode cmds", type(cmds))

        #Fill in defaults for username, password, and pkey
        if username is None:
            username = getpass.getuser()
        if password is None and pkey is None:
            pkey = f"{os.environ['HOME']}/.ssh/id_rsa"

        #Do not execute SSH if only localhost
        if len(self.hosts) == 0:
            do_ssh = False

        self.pkey = pkey
        self.password = password
        self.sudo=sudo
        self.username=username
        self.port = int(port)
        self.do_ssh = do_ssh
        self.exec_async = exec_async
        self.shell = shell

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
            pythonpath = """
            import sys
            while '' in sys.path:
                sys.path.remove('')
            print(":".join(sys.path))
            """
            self.cmds.insert(0, f"export PYTHONPATH=`python3 -c \"{pythonpath}\"`:$PATH")
            self.cmds.insert(1, f"source /home/{self.username}/.bashrc")
        if self.do_ssh:
            cmd = " ; ".join(self.cmds)
            self.output = self._exec_ssh(cmd)
        else:
            self.output = ExecNode('SSH Command', self.cmds, self.print_output, self.collect_output, shell=self.shell, sudo=self.sudo, exec_async=self.exec_async).Run().GetOutput()
        return self

    def __str__(self):
        return "SSHNode {}".format(self.name)
