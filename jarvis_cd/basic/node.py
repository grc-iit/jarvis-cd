from abc import ABC, abstractmethod
from jarvis_cd.basic.enumerations import Color, OutputStream
from jarvis_cd.basic.jarvis_manager import JarvisManager
import sys
import inspect

class Node(ABC):
    def __init__(self, print_output=True, collect_output=True, name=None):
        self.print_output = print_output
        self.name = name
        self.collect_output = collect_output
        self.print_fancy = JarvisManager.GetInstance().print_fancy

        self.output = {}
        self.class_params = {}
        self.AddHost('localhost')

    @abstractmethod
    def _Run(self):
        pass

    def Run(self):
        self._Run()
        if self.print_output:
            self.Print()
        return self

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
        self.output[host] = {
            OutputStream.STDOUT: [[], []],
            OutputStream.STDERR: [[], []],
            OutputStream.STDNULL: [[], []]
        }

    def CopyOutput(self, node, host):
        self.AddHost(host)
        self.output[host].update(node.output['localhost'])

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
    def GetLocalStdnull(self):
        return self.GetOutpu(host='localhost', stream=OutputStream.STDNULL)
    def GetLocalOutput(self):
        return self.GetOutput(host='localhost', stream=None)

    def SetClassParams(self, klass, kwargs):
        self.class_params[klass] = kwargs

    def _FindClassParams(self, klass, args):
        if not issubclass(klass, Node):
            return
        func = getattr(klass, '__init__')
        param_names = list(inspect.signature(func).parameters.keys())
        for param_name in param_names:
            if param_name == 'kwargs':
                continue
            if param_name == 'self':
                continue
            args[param_name] = getattr(self, param_name)
        for base in klass.__bases__:
            self._FindClassParams(base, args)

    def GetClassParams(self, klass=None, ignore_base=None, **override):
        if klass is None:
            klass = type(self)
        if klass not in self.class_params:
            args = {}
            self._FindClassParams(klass, args)
            self.SetClassParams(klass, args)
        if ignore_base is None and len(override) == 0:
            return self.class_params[klass]
        args = self.class_params[klass].copy()
        if ignore_base is not None:
            ignore_params = self.GetClassParams(klass=ignore_base)
            for param_name in ignore_params.keys():
                del args[param_name]
        args.update(override)
        return args

    def _GetParamStr(self, params):
        return ','.join([f"{key}=\'{val}\'" if isinstance(val, str) else f"{key}={val}" for key, val in params.items()])

    def _ToShellCmd(self, ignore_base=None, set_params={}):
        node_import = type(self).__module__
        node_params = self.GetClassParams(ignore_base=ignore_base, **set_params)
        node_type = type(self).__name__
        param_str = self._GetParamStr(node_params)
        python_cmds = [
            f"from {node_import} import {node_type}",
            f"from jarvis_cd.jarvis_manager import JarvisManager",
            "JarvisManager.GetInstance().DisableFancyPrint()",
            f"{node_type}({param_str}).Run()"
        ]
        python_cmd = '\n'.join(python_cmds)
        cmd = f"python3 -c \"{python_cmd}\" -C $JARVIS_ROOT"
        return cmd