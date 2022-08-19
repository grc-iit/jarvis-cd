import pickle as pkl
from jarvis_cd.serialize.serializer import Serializer

class PickleFile(Serializer):
    def __init__(self, path):
        self.path = path

    def Load(self):
        return pkl.load(self.path)

    def Save(self, data):
        with open(self.path, 'wb') as fp:
            pkl.dump(data, fp)