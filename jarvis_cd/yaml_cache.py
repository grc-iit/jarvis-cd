from jarvis_cd.serialize.yaml_file import YAMLFile
import os

class YAMLCache:
    def ScaffoldCachePath(self):
        return os.path.join(self.scaffold_dir, 'jarvis_cache.yaml')

    def LoadCache(self):
        if not os.path.exists(self.ScaffoldCachePath()):
            return None
        return YAMLFile(self.ScaffoldCachePath()).Load()

    def SaveCache(self):
        YAMLFile(self.ScaffoldCachePath()).Save(self.cache)