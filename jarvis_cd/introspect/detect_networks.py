import psutil
from jarvis_cd.node import Node

class DetectNetworks(Node):
    def __init__(self, print_output=True, collect_output=True):
        super().__init__(print_output=print_output, collect_output=collect_output)

    def _Run(self):
        self.net_cards = psutil.net_if_addrs()
        self.output[0]['localhost']['stdout'] = []
        for card,addrs in self.net_cards.items():
            self.output[0]['localhost']['stdout'].append(str(card))
            for addr in addrs:
                self.output[0]['localhost']['stdout'].append(f"  {addr.address}")