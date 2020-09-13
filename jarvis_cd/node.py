from abc import ABC, abstractmethod

from jarvis_cd.enumerations import Color


class Node(ABC):
    def __init__(self, name, print_output=False):
        self.print_output = print_output
        self.name = name

    def Print(self, output):
        for host in output:
            for line in output[host]['stdout']:
                print("[INFO] {host} {line}".format(host=host, line=line))
            for line in output[host]['stderr']:
                print(Color.RED + "[ERROR] {host} {line}".format(host=host, line=line)+ Color.END)

    @abstractmethod
    def Run(self):
        pass

    def __str__(self):
        return self.name