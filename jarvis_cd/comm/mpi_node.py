from jarvis_cd.basic.exec_node import ExecNode


class MPINode(ExecNode):
    def __init__(self, num_procs, cmd, hostfile=None, **kwargs):
        hostfile = ""
        if hostfile:
            hostfile = "-f {}".format(hostfile)
        exec_cmd = "mpirun -n {0} {1} {2}".format(num_procs, hostfile, cmd)
        super().__init__(exec_cmd, **kwargs)

    def __str__(self):
        return "MPINode {}".format(self.name)

