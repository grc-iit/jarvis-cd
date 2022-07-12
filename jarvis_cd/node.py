from abc import ABC, abstractmethod
from jarvis_cd.enumerations import Color, OutputStream
import inspect
import sys

class Node(ABC):
    def __init__(self, print_output=True, collect_output=True, print_fancy=True, name=None):
        self.print_output = print_output
        self.name = name
        self.collect_output = collect_output
        self.print_fancy = print_fancy
        self.output = {}
        self.AddHost('localhost')

    def Print(self):
        if self.print_fancy:
            for host,outputs in self.output.items():
                for line,color in zip(outputs[OutputStream.STDOUT][0], outputs[OutputStream.STDOUT][1]):
                    print(color + "[OUT] {host} {line}".format(host=host, line=line) + Color.END)
                for line,color in zip(outputs[OutputStream.STDERR][0], outputs[OutputStream.STDERR][1]):
                    print(color + "[ERR] {host} {line}".format(host=host, line=line) + Color.END)
        else:
            for host,outputs in self.output.items():
                for line,color in zip(outputs[OutputStream.STDOUT][0], outputs[OutputStream.STDOUT][1]):
                    print(color + line + Color.END)
                for line,color in zip(outputs[OutputStream.STDERR][0], outputs[OutputStream.STDERR][1]):
                    print(color + line + Color.END, file=sys.stderr)

    def AddHost(self, host):
        if host in self.output:
            return
        self.output = {host: {
            OutputStream.STDOUT: [[], []],
            OutputStream.STDERR: [[], []]
        }}

    def AddOutput(self, outputs, host='localhost', stream=OutputStream.STDOUT, color=None):
        if isinstance(outputs, str):
            outputs = outputs.splitlines()
        if color is None and stream == OutputStream.STDOUT:
            color = Color.GREEN
        if color is None and stream == OutputStream.STDERR:
            color = Color.RED
        self.AddHost(host)
        self.output[host][stream][0] += outputs
        self.output[host][stream][1] += [color]*len(outputs)

    def GetOutput(self, host=None, stream=None):
        if host is None:
            return self.output
        elif stream is None:
            stdout = [(OutputStream.STDOUT, line) for line in self.output[host][OutputStream.STDOUT][0]]
            stderr = [(OutputStream.STDERR, line) for line in self.output[host][OutputStream.STDERR][0]]
            return stdout + stderr
        else:
            return self.output[host][stream][0]
    def GetLocalStdout(self):
        return self.GetOutput(host='localhost', stream=OutputStream.STDOUT)
    def GetLocalStderr(self):
        return self.GetOutput(host='localhost', stream=OutputStream.STDERR)
    def GetLocalOutput(self):
        return self.GetOutput(host='localhost', stream=None)

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
        return ','.join([f"{key}=\'{val}\'" if isinstance(val, str) else f"{key}={val}" for key, val in params.items()])

    def _ToShellCmd(self, ignore_params=[], set_params={}):
        node_import = type(self).__module__
        node_params = self._GetParams()
        for param in ignore_params:
            if param in node_params:
                del node_params[param]
        node_params.update(set_params)
        if 'kwargs' in node_params:
            kwargs = node_params['kwargs']
            del node_params['kwargs']
            node_params.update(kwargs)
        node_type = type(self).__name__
        param_str = self._GetParamStr(node_params)
        node_import = f"from {node_import} import {node_type}"
        node_run = f"{node_type}({param_str}).Run()"
        cmd = f"jarvis-exec \"{node_import}\n{node_run}\""
        return cmd

    def Run(self):
        self._Run()
        if self.print_output:
            self.Print()
        return self

    def __str__(self):
        return self.name
