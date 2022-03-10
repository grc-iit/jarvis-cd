import argparse

from jarvis_cd.enumerations import OperationType, LogLevel


def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


class ArgumentParser(object):
    __instance = None

    @staticmethod
    def GetInstance():
        """ Static access method. """
        if ArgumentParser.__instance is None:
            ArgumentParser()
        return ArgumentParser.__instance

    def __init__(self):
        super().__init__()
        """ Virtually private constructor. """
        if ArgumentParser.__instance is not None:
            raise Exception("This class is a singleton!")
        else:
            ArgumentParser.__instance = self
        self.parser = argparse.ArgumentParser(description='Jarvis CD')
        self.parser.add_argument("launcher", metavar='launcher', type=str,
                                 help="The launcher for a program")
        self.parser.add_argument("operation", metavar='operation', type=OperationType, choices=list(OperationType),
                                 help="Operation for the launcher (e.g., start)")
        self.parser.add_argument("--config", metavar='configuration', default=None, type=str,
                                 help="Configuration for the program being launched (optional)")
        self.parser.add_argument("-ll","--log-level", default=LogLevel.ERROR, type=LogLevel,
                                 choices=list(LogLevel),
                                 help="Log level for execution")
        self.parser.add_argument("--log-path", type=str, default=None,
                                 help="Jarvis log file.")
        self.args = self.parser.parse_args()
        self._validate()

    def _validate(self):
        pass