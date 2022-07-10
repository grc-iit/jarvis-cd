from jarvis_cd.introspect.detect_networks import DetectNetworks
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.node import Node

class FindHostAliases(DetectNetworks):
    def __init__(self, hosts):
        #Hosts
        if isinstance(hosts, list):
            self.hosts = hosts
        elif isinstance(hosts, str):
            self.hosts = [hosts]
        elif isinstance(hosts, Hostfile):
            self.hosts = hosts.list()
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SCPNode hosts", type(hosts))
        self.host_aliases = ['localhost']
        super().__init__(print_output=False)

    def _Run(self):
        super()._Run()
        for card,addrs in self.net_cards.items():
            for addr in addrs:
                if addr.address in self.hosts:
                    self.host_aliases.append(addr.address)

    def GetAliases(self):
        return self.host_aliases

