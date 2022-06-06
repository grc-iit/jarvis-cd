from abc import ABC, abstractmethod

from jarvis_cd.enumerations import Color


class Node(ABC):
    def __init__(self, name, print_output=True, collect_output=True):
        self.print_output = print_output
        self.name = name
        self.collect_output = collect_output
        self.output = { "localhost": {
            "stdout": [""],
            "stderr": [""]
        }}

    def _format_output(self):
        if isinstance(self.output, dict):
            self.output = [self.output]

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

    def Run(self):
        self._Run()
        self._format_output()
        if self.print_output:
            self.Print()
        return self

    def __str__(self):
        return self.name
