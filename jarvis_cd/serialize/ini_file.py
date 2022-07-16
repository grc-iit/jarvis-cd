import configparser
from jarvis_cd.serialize.serializer import Serializer

class IniFile(Serializer):
    def __init__(self, path):
        self.path = path

    def Load(self):
        config = configparser.ConfigParser()
        config.read(self.path)
        return config

    def Save(self, data):
        with open(self.path, 'w') as fp:
            data.write(fp)