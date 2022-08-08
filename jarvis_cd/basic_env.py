from jarvis_cd.shell.copy_node import CopyNode
from jarvis_cd.fs.rm_node import RmNode
from jarvis_cd.shell.bash_env import BashEnv
import Inher
import os

class BasicEnvMixin:
    def GetEnvFilename(self):
        class_name = type(self).__name__
        filename = f"{class_name}-{self.config['Z_UUID']}.jarvis_env.sh"
        return filename

    def GetUserEnv(self):
        return os.path.join(self.jarvis_root, 'jarvis_envs', os.environ['USER'])

    def GetEnv(self):
        filename = self.GetEnvFilename()
        return os.path.join(self.GetUserEnv(), filename)

    def GetLocalEnv(self):
        filename = self.GetEnvFilename()
        return os.path.join(self.shared_dir, filename)

    def SaveEnv(self):
        env = self.env
        if env is None:
            return
        if isinstance(self.env, list):
            env = "\n".join(env)
        with open(self.GetLocalEnv(), 'w') as fp:
            fp.write(env)

    def RmEnv(self):
        RmNode('*.jarvis_env.sh').Run()

    def LoadEnv(self):
        if not os.path.exists(self.GetLocalEnv()):
            return
        CopyNode(self.GetLocalEnv(), self.GetEnv(), hosts=self.jarvis_env_hosts).Run()
        BashEnv(self.jarvis_env).Run()

    def UnloadEnv(self):
        if not os.path.exists(self.GetLocalEnv()):
            return
        RmNode(self.GetEnv(), hosts=self.jarvis_env_hosts, shell=True).Run()