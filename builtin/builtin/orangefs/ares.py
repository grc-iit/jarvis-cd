from jarvis_util import *
from .custom_kern import OrangefsCustomKern


class OrangefsAres(OrangefsCustomKern):
    def ares_start(self):
        self.custom_start()

    def ares_stop(self):
        cmd = [
            f'{self.ofs_path}/sbin/ares-orangefs-terminate',
            self.config['pfs_conf'],
            self.config['server_hosts_path'],
            self.config['client_hosts_path'],
            self.config['mount'],
        ]
        cmd = ' '.join(cmd)
        print(cmd)
        Exec(cmd, LocalExecInfo(env=self.env))
