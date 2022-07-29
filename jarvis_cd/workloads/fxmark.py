from jarvis_cd.shell.exec_node import ExecNode
import re

class FxMarkOp(Enum):
    MWCL = "MWCL"

class FxMark(ExecNode):
    def __init__(self, op, ncore, duration, io_dir, **kwargs):
        cmd = f"fxmark --type={op} --ncore={ncore} --duration={duration} --root={io_dir}"
        kwargs['sudo'] = True
        kwargs['shell'] = True
        super().__init__(cmd, **kwargs)