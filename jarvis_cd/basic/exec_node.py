import subprocess
import shlex
import time
import os,sys
import asyncio

from jarvis_cd.node import *

class ExecNode(Node):
    def __init__(self, name, cmds, print_output=False, collect_output=True, affinity=None, sleep_period_ms=100, max_retries=0,cwd=None, sudo=False, exec_async=False, shell=False):
        super().__init__(name, print_output, collect_output)
        self.cmds=cmds
        if isinstance(self.cmds, str):
            self.cmds = [self.cmds]
        self.proc = None
        self.affinity = affinity
        self.sleep_period_ms = sleep_period_ms
        self.max_retries = 0
        self.loop = None
        self.future = None
        self.stdout = None
        self.stderr = None
        self.cwd = cwd
        self.sudo = sudo
        self.exec_async = exec_async
        self.shell = shell

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
        if self.affinity:
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

    def _get_output(self):
        if self.collect_output:
            self.stdout, self.stderr = self.proc.communicate()
            self.output = { "localhost": {
                "stdout": [line.decode("utf-8") for line in self.stdout.splitlines()],
                "stderr": [line.decode("utf-8") for line in self.stderr.splitlines()]
            }}

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
        return self.output

    def GetExitCode(self):
        if self.proc is not None:
            return self.proc.returncode
        else:
            return None

    def Kill(self):
        if self.proc is not None:
            ExecNode(f"kill -9 {self.GetPid()}", collect_output=False).Run()
            self.proc.kill()
            return self.Wait()

    def GetPid(self):
        if self.proc is not None:
            return self.proc.pid
        else:
            return None

    async def _RunAsync(self):
        self.output = []
        retries = 0
        while True:
            time.sleep(self.sleep_period_ms / 1000)
            self.output = self._exec_cmds(self.cmds)
            if self.GetExitCode() == 0 or retries == self.max_retries:
                break
            retries += 1
            print(f"Retrying {self.cmds}")
        self.proc.wait()

    def RunAsync(self):
        self.loop = asyncio.get_event_loop()
        self.future = self.loop.create_task(self._RunAsync())
        return self

    def Wait(self):
        if self.loop is not None and self.future is not None:
            self.loop.run_until_complete(self.future)
            self.loop = None
            self.future = None
        return self

    def _Run(self):
        self.RunAsync()
        if not self.exec_async:
            self.Wait()
        return self

    def __str__(self):
        return "ExecNode {}".format(self.name)
