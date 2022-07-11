
from jarvis_cd.node import Node
from jarvis_cd.basic.exec_node import ExecNode
import re

class KillNode(Node):
    def __init__(self, program_regex):
        self.program_regex = program_regex
        super().__init__()

    def _Run(self):
        node = ExecNode('ps -ef', print_output=False).Run()
        pids = []
        for line in node.output[0]['localhost']['stdout']:
            words = line.split()
            if len(words) <= 7:
                continue
            cmd = " ".join(words[7:])
            if re.match(self.program_regex, cmd):
                pids.append(int(words[1]))
        if len(pids) > 0:
            for pid in pids:
                ExecNode(f"kill -9 {pid}", sudo=True).Run()
                self.output[0]['localhost']['stdout'].append(f"Killing {pid}")
        else:
            self.output[0]['localhost']['stdout'].append(f"No PIDs to kill")
