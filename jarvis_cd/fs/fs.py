from jarvis_cd.basic.exec_node import ExecNode

class DropCaches(ExecNode):
    def __init__(self, **kwargs):
        cmd = "sh -c \"sync; echo 3 > /proc/sys/vm/drop_caches\""
        super().__init__(cmd, sudo=True, **kwargs)

class ChownFS(ExecNode):
    def __init__(self, user, fs_path, **kwargs):
        cmd = f"chown -R {user} {fs_path}"
        super().__init__(cmd, sudo=True, **kwargs)

class UnmountFS(ExecNode):
    def __init__(self, dev_path, **kwargs):
        cmd = f"umount {dev_path}"
        super().__init__(cmd, sudo=True, **kwargs)

class MountFS(ExecNode):
    def __init__(self, dev_path, fs_path, **kwargs):
        cmd = f"mount {dev_path} {fs_path}"
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
