import sys,os
from jarvis_cd.exception import Error, ErrorCode
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
        sys.path.append(self.root)

    def GetLauncherClass(self, module_name, class_name):
        jarvis_cd = __import__('jarvis_cd.launchers.{}.package'.format(module_name))
        launchers = getattr(jarvis_cd, 'launchers')
        module = getattr(launchers, module_name)
        package = getattr(module, 'package')
        klass = getattr(package, class_name)
        return klass

    def GetLauncherPath(self, module_name):
        return os.path.join(self.root, 'jarvis_cd', 'launchers', module_name)

    def GetDefaultConfigPath(self, launcher_name):
        return os.path.join(self.GetLauncherPath(launcher_name), 'default.yaml')
