from jarvis_cd.launcher.launcher import Launcher
from jarvis_cd.fs.mkdir_node import MkdirNode
from abc import ABC,abstractmethod

class Application(Launcher):
    @abstractmethod
    def _DefineInit(self):
        pass

    @abstractmethod
    def _DefineStart(self):
        pass

    @abstractmethod
    def _DefineStop(self):
        pass

    @abstractmethod
    def _DefineClean(self):
        pass

    @abstractmethod
    def _DefineStatus(self):
        pass

    def Init(self):
        MkdirNode(self.shared_dir, hosts=self.all_hosts).Run()
        self._DefineInit()
        self.SaveCache()
        self.SaveEnv()

    def Start(self):
        self.LoadEnv()
        self._DefineStart()
        self.UnloadEnv()

    def Stop(self):
        self._DefineStop()

    def Clean(self):
        self._DefineClean()
        self.RmEnv()

    def Status(self):
        self._DefineStatus()

    def Restart(self):
        self.Stop()
        self.Start()

    def Reset(self):
        self.Stop()
        self.Clean()
        self.Init()
        self.Start()

    def Destroy(self):
        self.Stop()
        self.Clean()

    def Setup(self):
        self.Init()
        self.Start()