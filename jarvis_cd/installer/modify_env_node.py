from jarvis_cd.node import Node
from enum import Enum
import re

class ModifyEnvNodeOps(Enum):
    PREPEND = 'prepend'
    APPEND = 'append'
    REMOVE = 'remove'

class ModifyEnvNode(Node):
    def __init__(self, name, path, info, op, print_output=True, collect_output=False):
        super.__init__(name, print_output=print_output, collect_output=collect_output)
        self.path = path
        self.info = info
        self.op = op

    def _Run(self):
        if self.op == ModifyEnvNodeOps.PREPEND or self.op == ModifyEnvNodeOps.APPEND:
            self._Insert(self.info)
        else:
            self._Remove(self.info)

    def _Insert(self, cmds):
        if cmds is None:
            return
        if isinstance(cmds, str):
            cmds = [cmds]
        cmds = [f"{cmd}\n" for cmd in cmds]
        cmd = ''.join(cmds)
        with open(self.path, 'r') as fp:
            text = fp.read()
            text = cmd + text
        with open(self.path, 'w') as fp:
            fp.write(text)

    def _Remove(self, regexs):
        if regexs is None:
            return
        if isinstance(regexs, str):
            regexs = [regexs]
        with open(self.path, 'r') as fp:
            lines = fp.readlines()
            for line in lines:
                for regex in regexs:
                    if re.match(regex, line):
                        lines.remove(line)
                        break
        with open(self.path, 'w') as fp:
            text = '\n'.join(lines)
            fp.write(text)

