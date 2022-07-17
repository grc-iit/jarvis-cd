from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.shell.copy_node import CopyNode
from jarvis_cd.launcher.launcher import Launcher
from jarvis_cd.jarvis_manager import JarvisManager
from jarvis_cd.introspect.check_command import CheckCommandNode
from jarvis_cd.util.naming import ToCamelCase
import os

class Deps(Launcher):
    def _ProcessConfig(self):
        self.jarvis_env = os.path.join(JarvisManager.GetInstance().GetJarvisRoot(), '.jarvis_env')

    def _Scaffold(self):
        #Check if jarvis already installed
        if 'JARVIS_ROOT' in os.environ:
            self.config['jarvis_cd']['path'] = os.environ['JARVIS_ROOT']

        #Check if spack already installed
        node = CheckCommandNode('spack').Run()
        if node.Exists():
            self.config['spack']['path'] = os.environ['SPACK_ROOT']

        #Check if scsrepo already installed
        if 'SCS_REPO' in os.environ:
            self.config['scs_repo']['path'] = os.environ['SCS_REPO']

    def _DepsArgs(self, parser):
        parser.add_argument('package_name', metavar='pkg', type=str, help="name of package to install")

    def Install(self, package_name):
        if package_name == 'jarvis' or package_name == 'all':
            jarvis_root = self.config['jarvis_cd']['path']
            jarvis_conf = os.path.join(jarvis_root, 'jarvis_conf.yaml')
            GitNode(**self.config['jarvis_cd'], method=GitOps.CLONE, hosts=self.all_hosts, ssh_info=self.ssh_info).Run()
            CopyNode(jarvis_conf, jarvis_conf, hosts=self.all_hosts, ssh_info=self.ssh_info).Run()
            cmds = [
                f"chmod +x {jarvis_root}/dependencies.sh",
                f"{jarvis_root}/dependencies.sh",
                f"export PYTHONPATH={jarvis_root}",
                f"cd {self.config['jarvis_cd']['path']}",
                f"./bin/jarvis deps local-install {package_name}"
            ]
            ExecNode(cmds, hosts=self.all_hosts, ssh_info=self.ssh_info).Run()
        else:
            ExecNode(f"jarvis deps local-install {package_name}", hosts=self.all_hosts, ssh_info=self.ssh_info).Run()
    def _InstallArgs(self, package_name):
        self._DepsArgs(package_name)

    def Update(self, package_name):
        ExecNode(f"jarvis deps local-update {package_name}", hosts=self.all_hosts, ssh_info=self.ssh_info).Run()
    def _UpdateArgs(self, package_name):
        self._DepsArgs(package_name)

    def Uninstall(self, package_name):
        ExecNode(f"jarvis deps local-uninstall {package_name}", hosts=self.all_hosts, ssh_info=self.ssh_info).Run()
    def _UninstallArgs(self, package_name):
        self._DepsArgs(package_name)

    def LocalInstall(self, package_name):
        klass = self._GetPackageClass(package_name)
        klass().LocalInstall()
    def _LocalInstallArgs(self, package_name):
        self._DepsArgs(package_name)

    def LocalUpdate(self, package_name):
        klass = self._GetPackageClass(package_name)
        klass().LocalUpdate()
    def _LocalUpdate(self, package_name):
        self._DepsArgs(package_name)

    def LocalUninstall(self, package_name):
        klass = self._GetPackageClass(package_name)
        klass().LocalUninstall()
    def _LocalUninstall(self, package_name):
        self._DepsArgs(package_name)

    def _GetPackageClass(self, package_name):
        class_name = ToCamelCase(package_name)
        module = __import__(f"jarvis_cd.repos.deps.{package_name}_setup", fromlist=[class_name])
        klass = getattr(module, class_name)
        return klass