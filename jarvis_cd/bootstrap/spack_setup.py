import argparse
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.comm.ssh_node import SSHNode
from jarvis_cd.basic.exec_node import ExecNode
import sys,os

class SpackSetup:
    def __init__(self, argv):
        self.parser = argparse.ArgumentParser(description=f'Distributed spack installer')
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
        self.parser.add_argument("--host", metavar='ip_addr', default=None, type=str,
                                 help="The single host to bootstrap")
        self.parser.add_argument("--branch", metavar='name', default='releases/v0.18', type=str,
                                 help="The branch to switch to")
        self.parser.add_argument("--commit", metavar='hash', default=None, type=str,
                                 help="The hash to checkout")
        self.parser.add_argument("--repo", metavar='repo', default='https://github.com/spack/spack.git', type=str,
                                 help=f"The spack repo to clone")
        self.args = self.parser.parse_args(argv)

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
        self.branch = self.args.branch
        self.commit = self.args.commit
        self.repo = self.args.repo

    def DoArgs(self):
        if self.operation == 'install':
            self.Install()
        elif self.operation == 'update':
            self.Update()
        elif self.operation == 'uninstall':
            self.Uninstall()
        elif self.operation == 'reset_bashrc':
            self.ResetBashrc()

    def Install(self):
        priv_key = f'{self.key_dir}/{self.key_name}'
        # Create SSH directory on all nodes
        cmds = []
        cmds.append(f'git clone {self.repo}')
        cmds.append(f'cd spack')
        if self.commit is not None:
            cmds.append(f'git checkout {self.commit}')
        if self.branch is not None:
            cmds.append(f'git checkout {self.branch}')
        cmds.append(f'sed -i.old \"1s;^;. $HOME/share/spack/setup-env.sh\\n;" ~/.bashrc')
        SSHNode('Install spack', self.hosts, cmds, pkey=priv_key, username=self.username, port=self.port, collect_output=False).Run()

    def Update(self):
        priv_key = f'{self.key_dir}/{self.key_name}'
        cmds = []
        cmds.append(f'cd $SPACK_ROOT')
        if self.commit is not None:
            cmds.append(f'git checkout {self.commit}')
        if self.branch is not None:
            cmds.append(f'git pull origin {self.branch}')
            cmds.append(f'git checkout {self.branch}')
        else:
            cmds.append(f'git pull origin master')
        SSHNode('Update spack', self.hosts, cmds, pkey=priv_key, username=self.username, port=self.port, collect_output=False).Run()

    def Uninstall(self):
        priv_key = f'{self.key_dir}/{self.key_name}'
        cmds = [
            f'python3 $JARVIS_ROOT/bin/dspack spack reset_bashrc',
            f'rm -rf $SPACK_ROOT'
        ]
        SSHNode('Uninstall spack', self.hosts, cmds, pkey=priv_key, username=self.username, port=self.port, collect_output=False).Run()

    def ResetBashrc(self):
        with open(f'$HOME/.bashrc', 'r') as fp:
            bashrc = fp.read()
            bashrc = bashrc.replace(f'. $HOME/share/spack/setup-env.sh\n', '')
        with open(f'$HOME/.bashrc', 'w') as fp:
            fp.write(bashrc)