import argparse

from jarvis_cd.enumerations import OperationType


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
    def get_instance():
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
        self.parser = argparse.ArgumentParser(description='DLIO Benchmark')
        self.parser.add_argument("target", metavar='S', type=str,
                                 help="Target to choose.")
        self.parser.add_argument("operation", metavar='O', default=OperationType.DEPLOY, type=OperationType, choices=list(OperationType),
                                 help="Operation for target")
        self.args = self.parser.parse_args()
        self._validate()

    def _validate(self):
        pass