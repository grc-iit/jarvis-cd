from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.hostfile import Hostfile

class MPINode(ExecNode):
    def __init__(self, cmd, nprocs, hosts=None, **kwargs):
        if isinstance(hosts, Hostfile):
            hostfile = hosts.Path()
        elif isinstance(hosts, str):
            hostfile = hosts
        else:
            hostfile = None

        mpirun = []
        mpirun.append('mpirun')
        mpirun.append(f"-n {nprocs}")
        if hostfile is not None:
            mpirun.append(f"--hostfile {hostfile}")
        mpirun.append(f"{cmd}")
        mpirun = " ".join(mpirun)
        super().__init__(mpirun, **kwargs)
        self.nprocs = nprocs
        self.cmd = cmd
        self.hostfile = hostfile

    def __str__(self):
        return "MPINode {}".format(self.name)

