from pssh.clients import ParallelSSHClient
import sys
from jarvis_cd.parallel_node import ParallelNode
from jarvis_cd.exception import Error, ErrorCode
from jarvis_cd.enumerations import OutputStream
from jarvis_cd.shell.local_exec_node import LocalExecNode
import getpass

sys.stderr = sys.__stderr__

class SSHExecNode(ParallelNode):
    def __init__(self, cmds, **kwargs):

        """
        cmds is assumed to be a list of shell commands
        Do not use SSHExecNode directly, use ExecNode instead
        """
        super().__init__(**kwargs)

        #Make sure commands are a list
        if isinstance(cmds, list):
            self.cmds=cmds
        elif isinstance(cmds, str):
            self.cmds=[cmds]
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SSHExecNode cmds", type(cmds))

    def _exec_ssh_py(self, cmd):
        client = ParallelSSHClient(self.hosts, user=self.username, pkey=self.pkey, password=self.password,
                                   port=self.port)
        output = client.run_command(cmd, sudo=self.sudo)
        nice_output = dict()
        for host_output in output:
            host = host_output.host
            self.AddOutput(list(host_output.stdout), host=host, stream=OutputStream.STDOUT)
            self.AddOutput(list(host_output.stderr), host=host, stream=OutputStream.STDERR)
        return [nice_output]

    def _exec_ssh(self, cmd):
        nodes = []
        for host in self.hosts:
            ssh_cmd = [
                f"echo {self.password} | " if self.password else None,
                f"ssh",
                f"-tt" if self.sudo else None,
                f"-i {self.pkey}" if self.pkey is not None else None,
                f"-p {self.port}" if self.port is not None else None,
                f"{self.username}@{host}" if self.username is not None else host,
                f"\"{cmd}\""
            ]
            ssh_cmd = [cmd for cmd in ssh_cmd if cmd is not None]
            ssh_cmd = " ".join(ssh_cmd)
            node = LocalExecNode([ssh_cmd], exec_async=True, shell=True).Run()
            nodes.append((host, node))

        for host,node in nodes:
            node.Wait()
            self.CopyOutput(node, host)

    def _Run(self):
        if not self.do_ssh:
            return
        if self.exec_async:
            for i,cmd in enumerate(self.cmds):
                self.cmds[i] += '  > /dev/null 2>&1 &'
        if self.sudo:
            self.cmds.insert(0, f"source /home/{getpass.getuser()}/.bashrc")
        cmd = "\n".join(self.cmds)
        cmd = f"bash <<\EOF\n{cmd}\nEOF"
        if self.sudo:
            cmd = f"sudo -S {cmd}"
        self._exec_ssh(cmd)
        return self

    def __str__(self):
        return "SSHExecNode {}".format(self.name)
