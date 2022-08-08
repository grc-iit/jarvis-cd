
from jarvis_cd.shell.exec_node import ExecNode
import os,re

class BashEnv(ExecNode):
    def __init__(self, env_path, **kwargs):
        cmd = f"bash -c 'source {env_path}; env'"
        if 'print_output' not in kwargs:
            kwargs['print_output'] = False
        super().__init__(cmd, **kwargs)

    def _Run(self):
        return
        super()._Run()
        for line in self.GetLocalStdout():
            toks = line.split('=')
            if len(toks) and re.match('[a-zA-Z0-9_]+', toks[0]):
                var_name = toks[0]
                var_val = '='.join(toks[1:])
                print(f"{var_name}={var_val}")
                os.environ[var_name] = var_val