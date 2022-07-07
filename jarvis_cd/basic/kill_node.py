
from jarvis_cd.basic.exec_node import ExecNode
import re

class KillNode(ExecNode):
    def __init__(self, name, program_regex):
        self.program_regex = program_regex

    def _Run(self):
        node = ExecNode('Get Processes', 'ps -ef').Run()
        pids = []
        for line in node.output['localhost']['stdout']:
            words = line.split(' ')
            pid = int(words[1])
            cmd = " ".join(words[7:])
            if re.match(self.program_regex, cmd):
                pids.append(pid)
        if len(pids) > 0:
            for pid in pids:
                ExecNode('Kill', f"kill -9 {pid}", sudo=True)
                self.output['localhost']['stdout'].append(f"Killing {pid}")
        else:
            self.output['localhost']['stdout'].append(f"No PIDs to kill")
