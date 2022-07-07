from jarvis_cd.basic.exec_node import ExecNode

class LinkNode(ExecNode):
    def __init__(self, name, src, dst, print_output=False, collect_output=True, affinity=None, sleep_period_ms=100, max_retries=0,cwd=None, sudo=False, exec_async=False, shell=False):
        self.src = src
        self.dst = dst
        cmd = f"ln -s {self.src} {self.dst}"
        super().__init__(name, cmd, print_output=print_output, collect_output=collect_output, affinity=affinity, cwd=cwd, sudo=sudo)