import sys,os
from jarvis_cd.exception import Error, ErrorCode

class JarvisManager:
    instance_ = None

    @staticmethod
    def GetInstance():
        if JarvisManager.instance_ is None:
            JarvisManager.instance_ = JarvisManager()
        return JarvisManager.instance_

    def __init__(self):
        if 'JARVIS_CD_ROOT' not in os.environ:
            raise Error(ErrorCode.NOT_INSTALLED).format('JARVIS_CD_ROOT')
        if 'JARVIS_CD_TMP' not in os.environ:
            raise Error(ErrorCode.NOT_INSTALLED).format('JARVIS_CD_TMP')
        self.root = os.environ['JARVIS_CD_ROOT']
        self.tmp = os.environ['JARVIS_CD_TMP']
        sys.path.append(self.root)

    def GetTmpDir(self):
        if not os.path.exists(self.tmp):
            os.makedirs(self.tmp)
        return self.tmp

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
