from jarvis_cd.shell.exec_node import ExecNode
import re

#IO Summary: 2273368 ops, 113660.694 ops/s, (17486/17486 r/w), 413.2mb/s,    103us cpu/op,   0.1ms latency

class Filebench(ExecNode):
    def __init__(self, ini_path, **kwargs):
        cmd = f"filebench -f {ini_path}"
        kwargs['sudo'] = True
        kwargs['shell'] = True
        super().__init__(cmd, **kwargs)

    def Parse(self):
        self.result = {}
        for line in self.GetLocalStdout():
            if 'IO Summary' in line:
                line = re.sub("^.*IO Summary:", '', line)
                entries = line.split(sep=',')
                ops = entries[0].strip().split()[0]
                thrpt = entries[1].strip().split()[0]
                grp = re.search('\(([0-9.]+)/([0-9.]+)', entries[2])
                num_reads = grp.group(1)
                num_writes = grp.group(2)
                grp = re.search("([0-9.]+)", entries[3])
                bw = grp.group(1)
                grp = re.search("([0-9.]+)us", entries[4])
                cpu = grp.group(1)
                grp = re.search("([0-9.]+)ms", entries[5])
                latency = grp.group(1)

                self.result['ops'] = ops
                self.result['ops/s'] = thrpt
                self.result['num_reads'] = num_reads
                self.result['num_writes'] = num_writes
                self.results['mb/s'] = bw
                self.results['cpu/op'] = cpu
                self.results['ms'] = latency

        return self.result