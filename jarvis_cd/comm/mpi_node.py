from jarvis_cd.basic.exec_node import ExecNode


class MPINode(ExecNode):
    def __init__(self, name, num_procs, cmd, hostfile=None, print_output=True, collect_output=True):
        hostfile = ""
        if hostfile:
            hostfile = "-f {}".format(hostfile)
        exec_cmd = "mpirun -n {0} {1} {2}".format(num_procs, hostfile, cmd)
        super().__init__(name, exec_cmd, print_output, collect_output)

    def __str__(self):
        return "MPINode {}".format(self.name)

