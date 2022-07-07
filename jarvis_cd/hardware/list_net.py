import psutil
from jarvis_cd.node import Node

class DetectNetworks(Node):
    def __init__(self, name):
        super().__init__(name)

    def _Run(self):
        self.net_cards = psutil.net_if_addrs()
        self.output[0]['localhost']['stdout'] = []
        for card,addrs in self.net_cards.items():
            self.output[0]['localhost']['stdout'].append(str(card))
            for addr in addrs:
                self.output[0]['localhost']['stdout'].append(f"  {addr}")