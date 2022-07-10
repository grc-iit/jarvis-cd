
#USAGE jarvis-ssh [host-id]

from jarvis_cd.comm.ssh_config import SSHConfig
from jarvis_cd.comm.issh_node import InteractiveSSHNode
import sys,os

if __name__ == '__main__':
    conf = SSHConfig().LoadConfig()
    host_id = 0
    if len(sys.argv) == 2:
        host_id = int(sys.argv[1])
    if len(sys.argv) > 2:
        print("USAGE: jarvis-ssh [host-id]")
        exit(1)
    InteractiveSSHNode(conf.hosts[host_id], conf.ssh_info).Run()