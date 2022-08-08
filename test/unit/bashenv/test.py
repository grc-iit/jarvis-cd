
from jarvis_cd.shell.bash_env import BashEnv
from jarvis_cd.shell.exec_node import ExecNode
import os

print(os.environ['PATH'])
BashEnv(os.path.join(os.environ['HOME'], '.bashrc')).Run()
print(os.environ['PATH'])
ExecNode('mpirun --version').Run()
