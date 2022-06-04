from jarvis_cd.basic.exec_node import ExecNode

ExecNode("test", "echo hi", collect_output=False).Run()
ExecNode("test2",
         [
             "echo hi1",
             "echo hi2",
             "echo hi3"
         ], collect_output=False).Run()

single_cmd = ExecNode("test", "echo hi4", print_output=True)
single_cmd.Run()

many_cmd = ExecNode("test2",
         [
             "echo hi5",
             "echo hi6",
             "echo hi7"
         ], print_output=True)
many_cmd.Run()