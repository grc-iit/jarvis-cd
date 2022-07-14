import subprocess
import shlex
import time
import os,sys
import asyncio
from jarvis_cd.enumerations import Color, OutputStream

from jarvis_cd.basic.parallel_node import ParallelNode

class LocalExecNode(ParallelNode):
    def __init__(self, cmds, **kwargs):
        """
        cmds is assumed to be a list of shell commands
        Do not use LocalExecNode directly, use ExecNode instead
        """

        super().__init__(**kwargs)
        self.cmds = cmds
        self.proc = None
        self.loop = None
        self.future = None
        self.stdout = None
        self.stderr = None

    def _start_pipe_process(self, command, is_first=True):
        command_array = shlex.split(command)
        if not self.collect_output:
            self.proc = subprocess.Popen(command_array, cwd=self.cwd)
        elif is_first:
            self.proc = subprocess.Popen(command_array,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             cwd=self.cwd)
        else:
            self.proc = subprocess.Popen(command_array,
                             stdin=self.proc.stdout,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             cwd=self.cwd)
        if self.affinity is not None:
            os.sched_setaffinity(self.GetPid(), self.affinity)
        return self.proc

    def _start_bash_processes(self, commands):
        commands = " ; ".join(commands)
        if not self.collect_output:
            self.proc = subprocess.Popen(commands, cwd=self.cwd, shell=True)
        else:
            self.proc = subprocess.Popen(commands,
                                         stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE,
                                         cwd=self.cwd,
                                         shell=True)
        if self.affinity is not None:
            os.sched_setaffinity(self.GetPid(), self.affinity)

    def _get_output(self):
        if self.collect_output and not self.exec_async:
            self.stdout, self.stderr = self.proc.communicate()
            self.AddOutput([line.decode("utf-8") for line in self.stdout.splitlines()], stream=OutputStream.STDOUT)
            self.AddOutput([line.decode("utf-8") for line in self.stderr.splitlines()], stream=OutputStream.STDERR)

    def _exec_cmds(self,commands):
        """
        Executes a command on Shell and returns stdout and stderr from the command.
        :param commands: the string of the command to be executed
        :return: stdout: standard output of command , stderr  standard error of command
        """
        if self.sudo:
            commands = [f"sudo {command}" for command in commands]
        if self.shell:
            self._start_bash_processes(commands)
        else:
            for i, command in enumerate(commands):
                self._start_pipe_process(command, i==0)
        self._get_output()

    def GetExitCode(self):
        if self.proc is not None:
            return self.proc.returncode
        else:
            return None

    def Kill(self):
        if self.proc is not None:
            LocalExecNode(f"kill -9 {self.GetPid()}", collect_output=False).Run()
            self.proc.kill()

    def Wait(self):
        self.proc.wait()

    def GetPid(self):
        if self.proc is not None:
            return self.proc.pid
        else:
            return None

    def _Run(self):
        retries = 0
        while True:
            time.sleep(self.sleep_period_ms / 1000)
            self._exec_cmds(self.cmds)
            if self.GetExitCode() == 0 or retries == self.max_retries:
                break
            retries += 1
            print(f"Retrying {self.cmds}")

    def __str__(self):
        return "LocalExecNode {}".format(self.name)
