from jarvis_cd.exec_node import ExecNode
import re

class YCSB(ExecNode):
    def __init__(self, name, db_type, op_type, workload_path, data_dir,
        print_output=False, collect_output=True, affinity=None, sleep_period_ms=100, max_retries=0):
        if db_type == "rocksdb":
            db_dir_var = "rocksdb.dir"
        cmd = f"ycsb {op_type} {db_type} -s -P {workload_path} -p {db_dir_var}={data_dir}"
        super().__init__(name, cmd, print_output, collect_output, affinity, sleep_period_ms, max_retries)

    def GetRuntime():
        for host, line in self.output:
            grp = re.match("\[OVERALL\], RunTime\(ms\), ([0-9]+)", line)
            if grp:
                return float(grp.group(1))
