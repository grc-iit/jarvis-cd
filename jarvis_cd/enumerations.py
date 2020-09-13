import logging
from enum import Enum

class OperationType(Enum):
    START = 'start'
    STOP = 'stop'

    def __str__(self):
        return self.value

class Color:
    PURPLE = '\033[95m'
    CYAN = '\033[96m'
    DARKCYAN = '\033[36m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    END = '\033[0m'

class LogLevel(Enum):
    DEBUG=str(logging.DEBUG)
    INFO=str(logging.INFO)
    WARNING=str(logging.WARNING)
    ERROR=str(logging.ERROR)
    CRITICAL=str(logging.CRITICAL)

    def __str__(self):
        return str(self.value)