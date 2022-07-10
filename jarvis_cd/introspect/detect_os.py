
import re
from jarvis_cd.node import *

class DetectOSNode(Node):
    def __init__(self, program, print_output=True, collect_output=True):
        super().__init__(print_output, collect_output)
        self.program = program

    def _DetectOSType(self, lines):
        for line in lines:
            if "ID=" in line:
                if 'ubuntu' in line:
                    return 'ubuntu'
                elif 'centos' in line:
                    return 'centos'
                elif 'debian' in line:
                    return 'debian'

    def _DetectOSLikeType(self, lines):
        for line in lines:
            if "ID_LIKE=" in line:
                if 'ubuntu' in line:
                    return 'ubuntu'
                elif 'centos' in line:
                    return 'centos'
                elif 'debian' in line:
                    return 'debian'

    def _DetectOSVersion(self, lines):
        for line in lines:
            grp = re.match('VERSION_ID=\"(.*)\"')
            if grp:
                return grp.group(1)

    def _Run(self):
        with open('/etc/os-release') as fp:
            lines = fp.read().splitlines()
            self.os = self._DetectOSType()
            self.os_like = self._DetectOSLiketype()
            self.os_version = self._DetectOSVersion()

    def IsLike(self, os, version):
        if os == self.os or os == self.os_like:
            if version == self.os_version:
                return True
        return False