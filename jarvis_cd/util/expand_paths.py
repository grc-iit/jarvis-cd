
import os

class ExpandPaths:
    def __init__(self, config):
        self.config = config

    def _ExpandPath(self, path):
        return os.path.expandvars(path)

    def _ExpandDict(self, dict_var):
        return {key: self._ExpandVar(var) for key, var in dict_var.items()}

    def _ExpandList(self, list_var):
        return [self._ExpandVar(var) for var in list_var]

    def _ExpandVar(self, var):
        if isinstance(var, dict):
            return self._ExpandDict(var)
        if isinstance(var, list):
            return self._ExpandList(var)
        if isinstance(var, str):
            return self._ExpandPath(var)
        else:
            return var

    def Run(self):
        return self._ExpandVar(self.config)