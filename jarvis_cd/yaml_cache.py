from jarvis_cd.serialize.yaml_file import YAMLFile
import os

class YAMLCacheMixin:
    def ScaffoldCachePath(self):
        return os.path.join(self.shared_dir, 'jarvis_cache.yaml')

    def LoadCache(self):
        if not os.path.exists(self.ScaffoldCachePath()):
            return None
        return YAMLFile(self.ScaffoldCachePath()).Load()

    def SaveCache(self):
        if self.cache is None:
            return
        YAMLFile(self.ScaffoldCachePath()).Save(self.cache)