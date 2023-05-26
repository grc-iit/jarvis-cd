from jarvis_cd.basic.node import Interceptor
from jarvis_util import *


class MyRepo(Interceptor):
    def __init__(self):
        """
        Initialize paths

        requires_shared indicates whether this pkg needs to be shared or not.
        """
        super().__init__(requires_shared=False)

    def modify_env(self):
        """
        Modify the jarvis environment.

        :return: None
        """
        pass
