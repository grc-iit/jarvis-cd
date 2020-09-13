from enum import Enum

class OperationType(Enum):
    START = 'start'
    STOP = 'stop'

    def __str__(self):
        return self.value