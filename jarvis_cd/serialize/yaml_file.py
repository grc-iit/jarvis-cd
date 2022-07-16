from jarvis_cd.serialize.serializer import Serializer
import yaml

class YAMLFile(Serializer):
    def __init__(self, path):
        self.path = path

    def Load(self):
        with open(self.path, 'r') as fp:
            return yaml.load(fp, Loader=yaml.FullLoader)
        return None

    def Save(self, data):
        with open(self.path, 'w') as fp:
            yaml.dump(data, fp)
