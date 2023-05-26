from jarvis_cd.basic.node import Interceptor
from jarvis_util import *


class MyRepo(Interceptor):
    def __init__(self):
        """
        Initialize paths
        """
        super().__init__()

    def modify_env(self):
        """
        Modify the jarvis environment.

        :return: None
        """
        pass
