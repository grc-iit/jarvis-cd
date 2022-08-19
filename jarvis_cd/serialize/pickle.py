import pickle as pkl
from jarvis_cd.serialize.serializer import Serializer

class PickleFile(Serializer):
    def __init__(self, path):
        self.path = path

    def Load(self):
        with open(self.path, 'rb') as fp:
            return pkl.load(fp)

    def Save(self, data):
        with open(self.path, 'wb') as fp:
            pkl.dump(data, fp)