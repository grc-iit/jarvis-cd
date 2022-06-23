import argparse
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.comm.ssh_node import SSHNode
from jarvis_cd.comm.scp_node import SCPNode
import sys,os

class SSHSetup:
    def __init__(self, argv):
        self.parser = argparse.ArgumentParser(description='Setup SSH keys')
        self.parser.add_argument("--key", metavar='name', default='id_rsa', type=str,
                                 help="The name of the public/private key pair within the src_key_dir")
        self.parser.add_argument("--port", metavar='port', default=22, type=str,
                                 help="The port number for ssh")
        self.parser.add_argument("--user", metavar='username', default=os.environ['USER'], type=str,
                                 help="The username for ssh")
        self.parser.add_argument("--hosts", metavar='hostfile.txt', default=None, type=str,
                                 help="The set of all hosts to bootstrap")
        self.parser.add_argument("--host", metavar='ip_addr', default=None, type=str,
                                 help="The single host to bootstrap")
        self.parser.add_argument("--src_key_dir", metavar='path', default=None, type=str,
                                 help="Where to search for key pair on the current host")
        self.parser.add_argument("--dst_key_dir", metavar='path', default=None, type=str,
                                 help="Where to install key pair on destination hosts")
        self.parser.add_argument("--priv_key", metavar='bool', default=False, type=bool,
                                 help="Whether or not to install private key on hosts")
        self.args = self.parser.parse_args(argv)

    def ParseArgs(self):
        self.hosts = []
        if self.args.hosts is not None:
            self.hosts = Hostfile.LoadHostfile(self.args.hosts).list()
        if self.args.host is not None:
            self.hosts.append(self.args.host)
        if len(self.hosts) == 0:
            self.hosts = ['localhost']
        if self.args.src_key_dir is None:
            self.args.src_key_dir = os.path.join(os.environ['HOME'], '.ssh')
        if self.args.dst_key_dir is None:
            self.args.dst_key_dir = f'/home/{self.args.user}/.ssh'
        self.username = self.args.user
        self.port = self.args.port
        self.src_key_dir = self.args.src_key_dir
        self.dst_key_dir = self.args.dst_key_dir
        self.key_name = self.args.key
        self.do_priv_key = self.args.priv_key

    def DoArgs(self):
        self.TrustHosts()
        self.InstallKeys()
        self.SSHPermissions()

    def TrustHosts(self):
        # Ensure all self.hosts are trusted on this machine
        print("Connect to all self.hosts")
        for host in self.hosts:
            ExecNode('trust nodes', f'ssh -p {self.port} {self.username}@{host} echo init').Run()

    def InstallKeys(self):
        print("Install SSH keys")
        src_pub_key = f'{self.src_key_dir}/{self.key_name}.pub'
        src_priv_key = f'{self.src_key_dir}/{self.key_name}'
        dst_pub_key = f'{self.dst_key_dir}/{self.key_name}.pub'
        dst_priv_key = f'{self.dst_key_dir}/{self.key_name}'

        # Ensure pubkey trusted on all nodes
        for host in self.hosts:
            ExecNode('Install public key', f'ssh-copy-id -f -i {self.src_key_dir}/{self.key_name} -p {self.port} {self.username}@{host}',
                     collect_output=False).Run()
        # Create SSH directory on all nodes
        SSHNode('Make SSH directory', self.hosts, f'mkdir {self.dst_key_dir}', pkey=src_priv_key, username=self.username, port=self.port,
                collect_output=False).Run()

        # Copy the keys
        SCPNode('Copy public key to self.hosts', self.hosts, src_pub_key, dst_pub_key, pkey=src_priv_key, username=self.username,
                port=self.port,
                collect_output=False).Run()
        if self.do_priv_key:
            SCPNode('Copy private key to self.hosts', self.hosts, src_priv_key, dst_priv_key, pkey=src_priv_key,
                    username=self.username, port=self.port,
                    collect_output=False).Run()

    def _SSHPermissionsCmd(self, key_dir):
        commands = [
            f'chmod 700 {key_dir}',
            f'chmod 644 {key_dir}/{self.key_name}.pub',
            f'chmod 600 {key_dir}/{self.key_name}',
            f'chmod 600 {key_dir}/authorized_keys',
            f'chmod 644 {key_dir}/known_self.hosts',
            f'chmod 600 {key_dir}/config',
        ]
        return commands

    def SSHPermissions(self):
        src_priv_key = f'{self.src_key_dir}/{self.key_name}'
        src_cmd = self._SSHPermissionsCmd(self.src_key_dir)
        dst_cmd = self._SSHPermissionsCmd(self.dst_key_dir)
        ExecNode('Set permissions locally', src_cmd, collect_output=False).Run()
        SSHNode('Set permissions on destination', self.hosts, dst_cmd, pkey=src_priv_key, username=self.username, port=self.port,
                collect_output=False).Run()