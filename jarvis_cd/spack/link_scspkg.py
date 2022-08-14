
from jarvis_cd.shell.exec_node import ExecNode

class LinkScspkg(ExecNode):
    def __init__(self, pkg_name, link_path, **kwargs):
        self.pkg_name = pkg_name
        cmd = f"ln -s `scspkg pkg-root {pkg_name}` {link_path}"
        kwargs['shell'] = True
        super().__init__(cmd, **kwargs)