import subprocess
from shlex import shlex

from jarvis_cd.node import *

class ExecNode(Node):

    def __init__(self, cmd):
        self.cmd=cmd

    def _exec_cmd(command):
        """
        Executes a command on Shell and returns stdout and stderr from the command.
        :param command: the string of the command to be executed
        :return: stdout: standard output of command , stderr  standard error of command
        """
        command_array = shlex.split(command)
        out = subprocess.Popen(command_array,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)

        stdout, stderr = out.communicate()
        lines_b = stdout.splitlines()
        lines = []
        for line in lines_b:
            lines.append(line.decode("utf-8"))
        return lines, stderr

    def _exec_cmds(commands):
        """
        Executes a command on Shell and returns stdout and stderr from the command.
        :param commands: the string of the command to be executed
        :return: stdout: standard output of command , stderr  standard error of command
        """
        out = [None] * len(commands)
        prev = None

        for i, command in enumerate(commands):
            command_array = shlex.split(command)
            if i == 0:
                out[i] = subprocess.Popen(command_array,
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT)
            else:
                out[i] = subprocess.Popen(command_array,
                                          stdin=out[i - 1].stdout,
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT)
        stdout, stderr = out[len(commands) - 1].communicate()
        lines_b = stdout.splitlines()
        lines = []
        for line in lines_b:
            lines.append(line.decode("utf-8"))
        return lines, stderr

    def Run(self):
        if type(self.cmd) == list:
            return self._exec_cmds(self.cmd)
        else:
            return self._exec_cmd(self.cmd)

