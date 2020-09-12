from jarvis_cd.exec_node import ExecNode


class MPINode(ExecNode):
    def __init__(self, num_procs, cmd, hostfile=None):
        hostfile = ""
        if hostfile:
            hostfile = "-f {}".format(hostfile)
        exec_cmd = "mpirun -n {0} {1} {2}".format(num_procs, hostfile, cmd)
        super().__init__(exec_cmd)

    def Run(self):
        return super().Run()

