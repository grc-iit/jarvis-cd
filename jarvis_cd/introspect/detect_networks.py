import psutil
from jarvis_cd.node import Node
from jarvis_cd.enumerations import Color, OutputStream

class DetectNetworks(Node):
    def __init__(self, print_output=True, collect_output=True):
        super().__init__(print_output=print_output, collect_output=collect_output)

    def _Run(self):
        self.net_cards = psutil.net_if_addrs()
        for card,addrs in self.net_cards.items():
            self.AddOutput(card, stream=OutputStream.STDOUT)
            for addr in addrs:
                self.AddOutput(f"  {addr.address}", stream=OutputStream.STDOUT)