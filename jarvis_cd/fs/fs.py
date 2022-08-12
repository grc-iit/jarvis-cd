from jarvis_cd.shell.exec_node import ExecNode
from enum import Enum
import os

class DropCaches(ExecNode):
    def __init__(self, **kwargs):
        cmd = "sh -c \"sync; echo 3 > /proc/sys/vm/drop_caches\""
        super().__init__(cmd, sudo=True, **kwargs)

class ChownFS(ExecNode):
    def __init__(self, fs_path, user=None, **kwargs):
        if user is None:
            user = os.environ['USER']
        cmd = f"chown -R {user} {fs_path}"
        super().__init__(cmd, sudo=True, **kwargs)

class UnmountFS(ExecNode):
    def __init__(self, dev_path, **kwargs):
        cmd = f"umount {dev_path}"
        super().__init__(cmd, sudo=True, **kwargs)

class MountFS(ExecNode):
    def __init__(self, dev_path, fs_path, dax=False, **kwargs):
        cmd = [
            'mount',
            dev_path,
            fs_path
        ]
        if dax:
            cmd.append('-o dax')
        cmd = " ".join(cmd)
        super().__init__(cmd, sudo=True, **kwargs)

class EXT4Format(ExecNode):
    def __init__(self, dev_path, **kwargs):
        cmd = f"mkfs.ext4 {dev_path}"
        super().__init__(cmd, sudo=True, **kwargs)

class XFSFormat(ExecNode):
    def __init__(self, dev_path, **kwargs):
        cmd = f"mkfs.xfs {dev_path} -f"
        super().__init__(cmd, sudo=True, **kwargs)

class F2FSFormat(ExecNode):
    def __init__(self, dev_path, **kwargs):
        cmd = f"mkfs.f2fs {dev_path} -f"
        super().__init__(cmd, sudo=True, **kwargs)

class PrepareStorage(ExecNode):
    def __init__(self, spec, **kwargs):
        super().__init__(**kwargs)
        self.spec = spec
        self.nodes = []
        for item in self.spec:
            if item['format'] == 'EXT4':
                nodes.append(EXT4Format(**item['format_params'], **kwargs))
                nodes.append(MountFS(**item['mount_params'], **kwargs))
            if item['format'] == 'XFS':
                nodes.append(XFSFormat(**item['format_params'], **kwargs))
                nodes.append(MountFS(**item['mount_params'], **kwargs))
            if item['format'] == 'F2FS':
                nodes.append(F2FSFormat(**item['format_params'], **kwargs))
                nodes.append(MountFS(**item['mount_params'], **kwargs))

    def _Run(self):
        for node in self.nodes:
            node.Run()



class DisableVARandomization(ExecNode):
    def __init__(self, **kwargs):
        cmds = [
            "echo 0 | sudo tee /proc/sys/kernel/randomize_va_space"
        ]
        kwargs['shell'] = True
        super().__init__(cmds, **kwargs)


