import sys,os
from jarvis_cd.exception import Error, ErrorCode
from jarvis_cd.serialize.yaml_file import YAMLFile
from jarvis_cd.util.naming import ToSnakeCase
import pathlib


class JarvisManager:
    instance_ = None

    @staticmethod
    def GetInstance():
        if JarvisManager.instance_ is None:
            JarvisManager.instance_ = JarvisManager()
        return JarvisManager.instance_

    def __init__(self):
        self.root = os.path.dirname(pathlib.Path(__file__).parent.resolve())
        self.print_fancy = True
        self.ssh_info = None
        sys.path.insert(0,self.root)

    def DisableFancyPrint(self):
        self.print_fancy = False

    def EnableFancyPrint(self):
        self.print_fancy = True

    def SetSSHInfo(self, ssh_info):
        self.ssh_info = ssh_info

    def GetSSHInfo(self):
        return self.ssh_info


    def _LauncherPathTuple(self, module_name):
        module_name = ToSnakeCase(module_name)
        repos_path = os.path.join(self.root, 'jarvis_repos')
        for repo_name in os.listdir(repos_path):
            repo_path = os.path.join(repos_path, repo_name)
            if not os.path.isdir(repo_path):
                continue
            for some_module in os.listdir(repo_path):
                some_module_path = os.path.join(repo_path, some_module)
                if not os.path.isdir(some_module_path):
                    continue
                if some_module == module_name:
                    return (self.root, 'jarvis_repos', repo_name, module_name)
        return None

    def FindLauncherPath(self, module_name):
        path = self._LauncherPathTuple(module_name)
        if path is None:
            return None
        return os.path.join(*path)

    def GetRepoPath(self, repo_name):
        repo_name = ToSnakeCase(repo_name)
        return os.path.join(self.root, 'jarvis_repos', repo_name)

    def NewRepoPath(self, path):
        repo_name = os.path.basename(path)
        repo_name = ToSnakeCase(repo_name)
        dst_launcher_path = os.path.join(self.root, 'jarvis_repos', repo_name)
        return dst_launcher_path

    def GetLauncherClass(self, module_name, class_name):
        path = self._LauncherPathTuple(module_name)
        if path is None:
            return None
        jarvis_repos = __import__(f"{path[1]}.{path[2]}.{path[3]}.package", fromlist=[class_name])
        klass = getattr(jarvis_repos, class_name)
        return klass

    def GetJarvisRoot(self):
        return self.root

    def GetPkgInstanceDir(self, shared=False):
        if shared:
            if 'JARVIS_SHARED_PKG_DIR' in os.environ:
                return os.path.join(os.environ['JARVIS_SHARED_PKG_DIR'], 'jarvis_instances')
        else:
            if 'JARVIS_PER_NODE_PKG_DIR' in os.environ:
                return os.path.join(os.environ['JARVIS_PER_NODE_PKG_DIR'], 'jarvis_instances')
        return None