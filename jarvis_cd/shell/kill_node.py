from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.shell.jarvis_exec_node import JarvisExecNode
from jarvis_cd.enumerations import OutputStream
import re

class KillNode(JarvisExecNode):
    def __init__(self, program_regex, **kwargs):
        super().__init__(**kwargs)
        if not isinstance(program_regex, list):
            program_regex = [program_regex]
        self.program_regex = program_regex

    def _LocalRun(self):
        node = ExecNode('ps -ef', print_output=False).Run()
        pids = []
        for line in node.GetLocalStdout():
            words = line.split()
            if len(words) <= 7:
                continue
            #Split the command into tokens and check if each token matches a regex
            cmd = words[7:]
            cmd_matches = all([re.match(regex, cmd_token) is not None for regex,cmd_token in zip(self.program_regex,cmd)])
            if cmd_matches:
                pids.append(int(words[1]))
        if len(pids) > 0:
            for pid in pids:
                ExecNode(f"kill -9 {pid}", sudo=True).Run()
                self.AddOutput(f"Killing {pid}", stream=OutputStream.STDOUT)
        else:
            self.AddOutput(f"No PIDs to kill", stream=OutputStream.STDOUT)
