
import re, platform
from jarvis_cd.basic.node import *

class SystemInfoNode(Node):
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
            self.os = self._DetectOSType(lines)
            self.os_like = self._DetectOSLikeType(lines)
            self.os_version = self._DetectOSVersion(lines)
        self.ksemantic = platform.platform()
        self.krelease = platform.release()
        self.ktype = platform.system()
        self.cpu = platform.processor()
        self.cpu_family = platform.machine()

    def IsLike(self, os, version):
        if os == self.os or os == self.os_like:
            if version == self.os_version:
                return True
        return False

    def __hash__(self):
        return hash(str([self.os, self.os_like, self.os_version, self.ksemantic, self.krelease, self.ktype, self.cpu, self.cpu_family]))