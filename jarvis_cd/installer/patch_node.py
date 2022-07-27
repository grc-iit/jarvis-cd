
from jarvis_cd.shell.exec_node import ExecNode

class PatchNode(ExecNode):
    def __init__(self, patch_path, repo_dir, **kwargs):
        self.patch_path = patch_path
        self.repo_dir = repo_dir
        print(repo_dir)
        cmds = [
            f"cd {repo_dir}",
            f"git apply {patch_path}"
        ]
        kwargs['shell'] = True
        super().__init__(cmds, **kwargs)