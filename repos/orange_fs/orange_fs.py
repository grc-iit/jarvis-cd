from jarvis_cd.exception import Error, ErrorCode
from jarvis_cd.graph import Graph
import os

class OrangeFS(Graph):
    _default_config = "config/orangefs_sample.ini"
    def __init__(self, config_file = None):
        super().__init__(config_file);
        if not config_file:
            self.config.read(os.path.json())
        else:
            sections = self.config.sections()
            if len(sections) == 0:
                raise Error(ErrorCode.PATH_SECTION_REQUIRED, self._default_config)
    def _Define(self):



