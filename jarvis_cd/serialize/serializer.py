from abc import ABC,abstractmethod

class Serializer(ABC):
    @abstractmethod
    def Load(self):
        pass

    @abstractmethod
    def Save(self, data=None):
        pass