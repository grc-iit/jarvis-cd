
from csv import reader
from io import StringIO
import re
import pandas as pd

job_table = """
compute ares-comp-01
compute ares-comp-02
compute ares-comp-03
compute ares-comp-[04-05]
compute ares-comp-[05-06,07]
compute ares-comp-[08-09,10-11,12-13]
"""

def expand_nodelist(df):
    nodelist = []
    for row in df.itertuples():
        partition = row[1]
        node_id = row[2]
        compressed = re.search("(.*)-\[([0-9,\-]+)\]$", node_id)
        if compressed is None:
            nodelist.append((partition, node_id))
            continue
        prefix = compressed.group(1)
        postfix = compressed.group(2)
        pieces = postfix.split(",")
        for piece in pieces:
            seq = piece.split("-")
            if len(seq) == 1:
                nodelist.append((partition, "{}-{}".format(prefix,seq[0])))
            elif len(seq) == 2:
                lower = int(seq[0])
                upper = int(seq[1])
                for i in range(lower, upper+1):
                    nodelist.append((partition, "{}-{}".format(prefix,i)))
            else:
                raise Exception("Invalid notation to nodelist: {}".format(postfix))
    nodelist = pd.DataFrame(nodelist, columns=["partitions", "nodes"])
    return nodelist

df = pd.read_csv(StringIO(job_table), sep=" ", header=None).rename(columns={0:"partition", 1:"nodes"})
hostfile = expand_nodelist(df)
print(hostfile)
