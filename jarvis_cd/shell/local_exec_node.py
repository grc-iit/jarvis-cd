import subprocess
import shlex
import time
import os
from jarvis_cd.enumerations import OutputStream

from jarvis_cd.parallel_node import ParallelNode

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

    def _start_bash_processes(self, commands):
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

    def _get_env_str(self):
        envs = ['PATH', 'LD_LIBRARY_PATH', 'LIBRARY_PATH', 'CPATH']
        envs = [f"{key}={os.environ[key]}" for key in envs]
        return " ".join(envs)


    def _exec_cmds(self,commands):
        """
        Executes a command on Shell and returns stdout and stderr from the command.
        :param commands: the string of the command to be executed
        :return: stdout: standard output of command , stderr  standard error of command
        """

        if self.shell:
            commands = ' ; '.join(commands)
            if self.sudo:
                commands = f"sudo -E bash -c \'{self._get_env_str()} ; source {os.environ['HOME']}/.bashrc ; {commands}\'"
            print(commands)
            self._start_bash_processes(commands)
        else:
            if self.sudo:
                commands = [f"sudo -E {command}" for command in commands]
            for i, command in enumerate(commands):
                self._start_pipe_process(command, i==0)

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
        self.stdout, self.stderr = self.proc.communicate()
        if self.collect_output:
            self.AddOutput([line.decode("utf-8") for line in self.stdout.splitlines()], stream=OutputStream.STDOUT)
            self.AddOutput([line.decode("utf-8") for line in self.stderr.splitlines()], stream=OutputStream.STDERR)
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
        if not self.exec_async:
            self.Wait()

    def __str__(self):
        return "LocalExecNode {}".format(self.name)
