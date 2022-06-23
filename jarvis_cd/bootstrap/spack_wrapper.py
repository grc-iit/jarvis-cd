import argparse
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.comm.ssh_node import SSHNode
from jarvis_cd.basic.exec_node import ExecNode
import sys,os

class SpackWrapper:
    def __init__(self, argv):
        self.parser = argparse.ArgumentParser(description='dspack spack wrapper')
        self.parser.add_argument("operation", metavar='operation', default='id_rsa', type=str,
                                 help="The operation to perform (install/uninstall/update)")
        self.parser.add_argument("--key_dir", metavar='path', default=f'{os.environ["HOME"]}/.ssh', type=str,
                                 help="The directory where to search for keys")
        self.parser.add_argument("--key", metavar='name', default='id_rsa', type=str,
                                 help="The name of the public/private key pair within the key_dir")
        self.parser.add_argument("--port", metavar='port', default=22, type=str,
                                 help="The port number for ssh")
        self.parser.add_argument("--user", metavar='username', default=os.environ['USER'], type=str,
                                 help="The username for ssh")
        self.parser.add_argument("--hosts", metavar='hostfile.txt', default=None, type=str,
                                 help="The set of all hosts to bootstrap")
        self.args = argv

    def ParseArgs(self):
        self.hosts = []
        if self.args.hosts is not None:
            self.hosts = Hostfile.LoadHostfile(self.args.hosts).list()
        if self.args.host is not None:
            self.hosts.append(self.args.host)
        if len(self.hosts) == 0:
            self.hosts = ['localhost']
        if self.args.key_dir is None:
            self.args.key_dir = os.path.join(os.environ['HOME'], '.ssh')
        if self.args.key_dir is None:
            self.args.key_dir = f'/home/{self.args.user}/.ssh'
        self.operation = self.args.operation
        self.username = self.args.user
        self.port = self.args.port
        self.key_dir = self.args.key_dir
        self.key_name = self.args.key

    def DoArgs(self):
        if self.operation == 'prefix':
            self.ModulePath()
        else:
            self.Call()

    def Call(self):
        priv_key = f'{self.key_dir}/{self.key_name}'
        # Create SSH directory on all nodes
        self.cmd = f'spack {self.operation}'
        SSHNode('Execute distributed spack', self.hosts, cmds, pkey=priv_key, username=self.username, port=self.port,
                collect_output=False).Run()

    def ModulePath(spack_query):
        spack_mod_path = None
        spack_mod_path_stdout = ExecNode('spack path', f'spack find --paths {spack_query}').Run().output[0]['localhost']['stdout']
        for line in spack_mod_path_stdout:
            grp = re.search(f'({os.environ["SPACK_ROOT"]}.*)', line)
            if grp:
                spack_mod_path = grp.group(1)
        print(spack_mod_path)