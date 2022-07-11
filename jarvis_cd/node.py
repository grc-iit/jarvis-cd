from abc import ABC, abstractmethod
from jarvis_cd.enumerations import Color
import inspect

class Node(ABC):
    def __init__(self, print_output=True, collect_output=True, name=None):
        self.print_output = print_output
        self.name = name
        self.collect_output = collect_output
        self.output = [{ "localhost": {
            "stdout": [""],
            "stderr": [""]
        }}]

    def Print(self):
        #For each command
        for host_outputs in self.output:
            #Print all host outputs
            for host,outputs in host_outputs.items():
                for line in outputs['stdout']:
                    print("[INFO] {host} {line}".format(host=host, line=line))
                for line in outputs['stderr']:
                    print(Color.RED + "[ERROR] {host} {line}".format(host=host, line=line)+ Color.END)

    def GetOutput(self):
        return self.output

    @abstractmethod
    def _Run(self):
        pass

    def _GetParams(self, klass=None, func='__init__'):
        if klass is None:
            klass = self
        func = getattr(klass, func)
        params = list(func.__code__.co_varnames)
        if 'self' in params:
            params.remove('self')
        return { param: getattr(self, param) for param in params }

    def _GetParamStr(self, params):
        return ','.join([f"{key}=\"{val}\"" if isinstance(val, str) else f"{key}={val}" for key, val in params.items()])

    def _ToShellCmd(self):
        node_import = type(self).__module__
        node_params = self._GetParams()
        if 'kwargs' in node_params:
            kwargs = node_params['kwargs']
            del node_params['kwargs']
            node_params.update(kwargs)
        node_type = type(self).__name__
        param_str = self._GetParamStr(node_params)
        node_import = f"from {node_import} import {node_type}"
        node_run = f"{node_type}({param_str}).Run()"
        cmd = f"python3 -c \"{node_import}\n{node_run}\""
        return cmd

    def Run(self):
        self._Run()
        if self.print_output:
            self.Print()
        return self

    def __str__(self):
        return self.name
