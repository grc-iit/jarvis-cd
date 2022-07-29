from jarvis_cd.shell.exec_node import ExecNode
import re

class FIO(ExecNode):
    def __init__(self, ini_path, **kwargs):
        cmd = f"fio {ini_path}"
        super().__init__(cmd, **kwargs)

    def GetRuntime(self):
        for line in self.GetLocalStdout():
            grp = re.match("\[OVERALL\], RunTime\(ms\), ([0-9]+)", line)
            if grp:
                return float(grp.group(1))