from jarvis_cd.shell.exec_node import ExecNode

ExecNode("echo hi", collect_output=False).Run()
ExecNode([
             "echo hi1",
             "echo hi2",
             "echo hi3"
         ], collect_output=False).Run()

single_cmd = ExecNode("test", "echo hi4", print_output=True)
single_cmd.Run()

many_cmd = ExecNode(
         [
             "echo hi5",
             "echo hi6",
             "echo hi7"
         ], print_output=True)
many_cmd.Run()