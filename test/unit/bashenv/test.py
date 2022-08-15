
from jarvis_cd.shell.exec_node import ExecNode
import os

print(os.environ['PATH'])
print(os.environ['PATH'])
ExecNode('mpirun --version').Run()
