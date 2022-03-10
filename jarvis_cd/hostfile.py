import os
from jarvis_cd.exception import Error, ErrorCode

class Hostfile:
    def __init__(self):
        self.filename = None
        self.all_hosts = None
        self.hosts = None

    def LoadHostfile(self, filename):
        if not os.path.exists(filename):
            raise Error(ErrorCode.HOSTFILE_NOT_FOUND).format(filename)
        a_file = open(filename, "r")
        list_of_lists = []
        for line in a_file:
            stripped_line = line.strip()
            line_list = stripped_line.split(sep=":")
            if len(line_list) == 2:
                for i in range(int(line_list[1])):
                    list_of_lists.append(line_list[0])
            else:
                list_of_lists.append(line_list[0])
        a_file.close()
        self.all_hosts = list_of_lists
        self.hosts = self.all_hosts
        self.filename = filename
        return self

    def SelectHosts(self, count):
        if count > len(self.all_hosts):
            raise Error(ErrorCode.TOO_MANY_HOSTS_CHOSEN).format(self.filename, count, len(self.all_hosts))
        self.hosts = self.all_hosts[0:count]

    def list(self):
        return self.hosts

    def enumerate(self):
        return enumerate(self.hosts)

    def to_str(self, sep=','):
        return sep.join(self.hosts)

    def __len__(self):
        return len(self.hosts)

    def __getitem__(self, idx):
        return self.hosts[idx]