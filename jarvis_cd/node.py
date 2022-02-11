from abc import ABC, abstractmethod

from jarvis_cd.enumerations import Color


class Node(ABC):
    def __init__(self, name, print_output=False, collect_output=True):
        self.print_output = print_output
        self.name = name
        self.collect_output = collect_output

    def Print(self, host_outputs):
        for host,outputs in host_outputs.items():
            for line in outputs['stdout']:
                print("[INFO] {host} {line}".format(host=host, line=line))
            for line in outputs['stderr']:
                print(Color.RED + "[ERROR] {host} {line}".format(host=host, line=line)+ Color.END)

    @abstractmethod
    def Run(self):
        pass

    def __str__(self):
        return self.name