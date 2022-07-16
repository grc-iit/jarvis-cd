from jarvis_cd.node import Node
from enum import Enum
import re
import os,sys

class EnvNodeOps(Enum):
    SET = 'set'
    REMOVE = 'remove'

class EnvNode(Node):
    def __init__(self, path, op, cmd, cmd_re, **kwargs):
        super().__init__(**kwargs)
        self.path = path
        self.op = op
        self.cmd = cmd
        self.cmd_re = cmd_re

    def _Run(self):
        #Load the shell file text
        text = ''
        if os.path.exists(self.path):
            with open(self.path, 'r') as fp:
                text = fp.read()
        lines = text.splitlines()

        #Perform operation on content
        if self.op == EnvNodeOps.SET:
            self._RemoveMem(lines)
            self._AppendMem(lines)
        elif self.op == EnvNodeOps.REMOVE:
            self._RemoveMem(lines)

        #Save the updated shell text
        text = "\n".join(lines)
        with open(self.path, 'w') as fp:
            fp.write(text)

    def _RemoveMem(self, lines):
        for line in lines:
            if re.match(self.cmd_re, line):
                lines.remove(line)

    def _AppendMem(self, lines):
        lines.append(self.cmd)