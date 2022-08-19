from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.basic.parallel_node import ParallelNode
from jarvis_cd.fs.mkdir_node import MkdirNode
from enum import Enum
import os

class DropCaches(ExecNode):
    def __init__(self, **kwargs):
        cmd = "sh -c \"sync; echo 3 > /proc/sys/vm/drop_caches\""
        kwargs['sudo'] = True
        super().__init__(cmd, **kwargs)

class ChownFS(ExecNode):
    def __init__(self, fs_path, user=None, **kwargs):
        if user is None:
            user = os.environ['USER']
        cmd = f"chown -R {user} {fs_path}"
        kwargs['sudo'] = True
        super().__init__(cmd, **kwargs)

class ChmodFS(ExecNode):
    def __init__(self, path, perm, **kwargs):
        cmd = f"chmod {perm} {path}"
        super().__init__(cmd, **kwargs)

class UnmountFS(ExecNode):
    def __init__(self, dev_path, **kwargs):
        cmd = f"umount {dev_path}"
        kwargs['sudo'] = True
        super().__init__(cmd, **kwargs)

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
        kwargs['sudo'] = True
        super().__init__(cmd, **kwargs)

class EXT4Format(ExecNode):
    def __init__(self, dev_path, **kwargs):
        cmd = f"mkfs.ext4 {dev_path}"
        kwargs['sudo'] = True
        super().__init__(cmd, **kwargs)

class XFSFormat(ExecNode):
    def __init__(self, dev_path, **kwargs):
        cmd = f"mkfs.xfs {dev_path} -f"
        kwargs['sudo'] = True
        super().__init__(cmd, **kwargs)

class F2FSFormat(ExecNode):
    def __init__(self, dev_path, **kwargs):
        cmd = f"mkfs.f2fs {dev_path} -f"
        kwargs['sudo'] = True
        super().__init__(cmd, **kwargs)

class TmpfsMount(ExecNode):
    def __init_(self, size, fs_path, **kwargs):
        cmd = f"mount -t tmpfs -o size={size} tmpfs {fs_path}"
        kwargs['sudo'] = True
        super().__init__(cmd, **kwargs)

class PrepareStorage(ParallelNode):
    def __init__(self, spec, **kwargs):
        super().__init__(**kwargs)
        self.spec = spec
        self.nodes = []
        self.kwargs = kwargs

    def _Run(self):
        for item in self.spec:
            if item['format'] == 'EXT4':
                EXT4Format(**item['format_params'], **self.kwargs).Run()
                if 'mount_params' in item:
                    MkdirNode(item['mount_params']['fs_path'], **self.kwargs).Run()
                    MountFS(**item['mount_params'], **self.kwargs).Run()
                    ChownFS(item['mount_params']['fs_path'], **self.kwargs).Run()
            if item['format'] == 'XFS':
                XFSFormat(**item['format_params'], **self.kwargs).Run()
                if 'mount_params' in item:
                    MkdirNode(item['mount_params']['fs_path'], **self.kwargs).Run()
                    MountFS(**item['mount_params'], **self.kwargs).Run()
                    ChownFS(item['mount_params']['fs_path'], **self.kwargs).Run()
            if item['format'] == 'F2FS':
                F2FSFormat(**item['format_params'], **self.kwargs).Run()
                if 'mount_params' in item:
                    MkdirNode(item['mount_params']['fs_path'], **self.kwargs).Run()
                    MountFS(**item['mount_params'], **self.kwargs).Run()
                    ChownFS(item['mount_params']['fs_path'], **self.kwargs).Run()
            if item['format'] == 'tmpfs':
                MkdirNode(item['mount_params']['fs_path'], **self.kwargs).Run()
                TmpfsMount(**item['mount_params'], **self.kwargs).Run()
                ChownFS(item['mount_params']['fs_path'], **self.kwargs).Run()

class UnprepareStorage(ParallelNode):
    def __init__(self, spec, **kwargs):
        super().__init__(**kwargs)
        self.spec = spec
        self.nodes = []
        self.kwargs = kwargs

    def _Run(self):
        for item in self.spec:
            if item['format'] == 'EXT4':
                if 'mount_params' in item:
                    UnmountFS(item['mount_params']['fs_path'], **self.kwargs).Run()
            if item['format'] == 'XFS':
                if 'mount_params' in item:
                    UnmountFS(item['mount_params']['fs_path'], **self.kwargs).Run()
            if item['format'] == 'F2FS':
                if 'mount_params' in item:
                    UnmountFS(item['mount_params']['fs_path'], **self.kwargs).Run()
            if item['format'] == 'tmpfs':
                UnmountFS(item['mount_params']['fs_path'], **self.kwargs).Run()

class DisableVARandomization(ExecNode):
    def __init__(self, **kwargs):
        cmds = [
            "echo 0 | sudo tee /proc/sys/kernel/randomize_va_space"
        ]
        kwargs['shell'] = True
        super().__init__(cmds, **kwargs)


