from jarvis_cd.node import Node
from enum import Enum
import re
import os,sys

class ModifyEnvNodeOps(Enum):
    PREPEND = 'prepend'
    APPEND = 'append'
    REMOVE = 'remove'

class ModifyEnvNode(Node):
    def __init__(self, path, info, op, print_output=True, collect_output=False):
        super().__init__(print_output=print_output, collect_output=collect_output)
        self.path = path
        self.info = info
        self.op = op

    def _Run(self):
        if self.op == ModifyEnvNodeOps.APPEND:
            self._Insert(self.info)
        else:
            self._Remove(self.info)

    def _Insert(self, cmds):
        if cmds is None:
            return
        if isinstance(cmds, str):
            cmds = [cmds]
        cmd = '\n'.join(cmds)
        text = ''
        if os.path.exists(self.path):
            with open(self.path, 'r') as fp:
                text = fp.read()
        if len(text):
            text += '\n' + cmd
        else:
            text = cmd
        with open(self.path, 'w') as fp:
            fp.write(text)

    def _Remove(self, regexs):
        if regexs is None:
            return
        if not os.path.exists(self.path):
            return
        if isinstance(regexs, str):
            regexs = [regexs]
        with open(self.path, 'r') as fp:
            lines = fp.read().splitlines()
            for line in lines:
                for regex in regexs:
                    if re.match(regex, line):
                        lines.remove(line)
                        break
        with open(self.path, 'w') as fp:
            text = '\n'.join(lines)
            fp.write(text)

