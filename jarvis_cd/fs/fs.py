from jarvis_cd.exec_node import ExecNode

class DropCaches(ExecNode):
    def __init__(self, name, print_output=False, collect_output=True, affinity=None, sleep_period_ms=100, max_retries=0):
        cmd = "sudo sh -c \"sync; echo 3 > /proc/sys/vm/drop_caches\""
        super().__init__(name, cmd, print_output, collect_output, affinity, sleep_period_ms, max_retries)

class ChownFS(ExecNode):
    def __init__(self, name, user, fs_path, print_output=False, collect_output=True, affinity=None, sleep_period_ms=100, max_retries=0):
        cmd = f"sudo chown -R {user} {fs_path}"
        super().__init__(name, cmd, print_output, collect_output, affinity, sleep_period_ms, max_retries)

class UnmountFS(ExecNode):
    def __init__(self, name, dev_path, print_output=False, collect_output=True, affinity=None, sleep_period_ms=100, max_retries=0):
        cmd = f"sudo umount {dev_path}"
        super().__init__(name, cmd, print_output, collect_output, affinity, sleep_period_ms, max_retries)

class MountFS(ExecNode):
    def __init__(self, name, dev_path, fs_path, print_output=False, collect_output=True, affinity=None, sleep_period_ms=100, max_retries=0):
        cmd = f"sudo mount {dev_path} {fs_path}"
        super().__init__(name, cmd, print_output, collect_output, affinity, sleep_period_ms, max_retries)

class EXT4Format(ExecNode):
    def __init__(self, name, dev_path, print_output=False, collect_output=True, affinity=None, sleep_period_ms=100, max_retries=0):
        cmd = f"sudo mkfs.ext4 {dev_path}"
        super().__init__(name, cmd, print_output, collect_output, affinity, sleep_period_ms, max_retries)

class XFSFormat(ExecNode):
    def __init__(self, name, dev_path, print_output=False, collect_output=True, affinity=None, sleep_period_ms=100, max_retries=0):
        cmd = f"sudo mkfs.xfs {dev_path} -f"
        super().__init__(name, cmd, print_output, collect_output, affinity, sleep_period_ms, max_retries)

class F2FSFormat(ExecNode):
    def __init__(self, name, dev_path, print_output=False, collect_output=True, affinity=None, sleep_period_ms=100, max_retries=0):
        cmd = f"sudo mkfs.f2fs {dev_path} -f"
        super().__init__(name, cmd, print_output, collect_output, affinity, sleep_period_ms, max_retries)
