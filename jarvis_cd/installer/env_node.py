from jarvis_cd.node import Node
from jarvis_cd.shell.jarvis_exec_node import JarvisExecNode
from enum import Enum
import re
import os,sys

class EnvNodeOps(Enum):
    SET = 'set'
    REMOVE = 'remove'

class EnvNode(JarvisExecNode):
    def __init__(self, path, op, cmd=None, cmd_re=None, **kwargs):
        super().__init__(**kwargs)
        if cmd_re is None:
            cmd_re = cmd
        self.path = path
        self.op = op
        self.cmd = cmd
        self.cmd_re = cmd_re

    def _LocalRun(self):
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