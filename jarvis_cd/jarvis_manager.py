import sys,os
from jarvis_cd.exception import Error, ErrorCode
from jarvis_cd.serialize.yaml_file import YAMLFile
import pathlib

import importlib.util
import sys


class JarvisManager:
    instance_ = None

    @staticmethod
    def GetInstance():
        if JarvisManager.instance_ is None:
            JarvisManager.instance_ = JarvisManager()
        return JarvisManager.instance_

    def __init__(self):
        self.root = os.path.dirname(pathlib.Path(__file__).parent.resolve())
        sys.path.insert(0,self.root)

    def _LauncherPathTuple(self, module_name):
        repos_path = os.path.join(self.root, 'jarvis_repos')
        for namespace in os.listdir(repos_path):
            namespace_path = os.path.join(repos_path, namespace)
            if not os.path.isdir(namespace_path):
                continue
            for some_module in os.listdir(namespace_path):
                some_module_path = os.path.join(namespace_path, some_module)
                if not os.path.isdir(some_module_path):
                    continue
                if some_module == module_name:
                    return (self.root, 'jarvis_repos', namespace, module_name)
        return None

    def FindLauncherPath(self, module_name):
        path = self._LauncherPathTuple(module_name)
        if path is None:
            return None
        os.path.join(*path)

    def GetLauncherClass(self, module_name, class_name):
        path = self._LauncherPathTuple(module_name)
        if path is None:
            return None
        jarvis_repos = __import__(f"{path[1]}.{path[2]}.{path[3]}.package", fromlist=[class_name])
        klass = getattr(jarvis_repos, class_name)
        return klass

    def GetJarvisRoot(self):
        return self.root