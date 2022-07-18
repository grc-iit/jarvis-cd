from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.shell.copy_node import CopyNode
from jarvis_cd.launcher.launcher import Launcher
from jarvis_cd.jarvis_manager import JarvisManager
from jarvis_cd.introspect.check_command import CheckCommandNode
from jarvis_cd.util.naming import ToCamelCase
import os

class Deps(Launcher):
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
            GitNode(**self.config['jarvis_cd'], method=GitOps.CLONE, hosts=self.all_hosts).Run()
            CopyNode(jarvis_conf, jarvis_conf, hosts=self.all_hosts).Run()
            cmds = [
                f"cd {jarvis_root}",
                f"chmod +x {jarvis_root}/dependencies.sh",
                f"{jarvis_root}/dependencies.sh",
                f"export PYTHONPATH={jarvis_root}",
                f"./bin/jarvis deps local-install {package_name}"
            ]
            ExecNode(cmds, hosts=self.all_hosts).Run()
        else:
            ExecNode(f"jarvis deps local-install {package_name} -C {self.jarvis_root}", hosts=self.all_hosts).Run()
    def _InstallArgs(self, package_name):
        self._DepsArgs(package_name)

    def Update(self, package_name):
        ExecNode(f"jarvis deps local-update {package_name} -C {self.jarvis_root}", hosts=self.all_hosts).Run()
    def _UpdateArgs(self, package_name):
        self._DepsArgs(package_name)

    def Uninstall(self, package_name):
        ExecNode(f"jarvis deps local-uninstall {package_name} -C {self.jarvis_root}", hosts=self.all_hosts).Run()
    def _UninstallArgs(self, package_name):
        self._DepsArgs(package_name)

    def _PackageSet(self, package_name):
        if package_name != 'all':
            return [package_name]
        else:
            return ['jarvis', 'scs_repo', 'spack']

    def LocalInstall(self, package_name):
        for package in self._PackageSet(package_name):
            pkg = self._GetPackageInstance(package)
            pkg.LocalInstall()
    def _LocalInstallArgs(self, package_name):
        self._DepsArgs(package_name)

    def LocalUpdate(self, package_name):
        for package in self._PackageSet(package_name):
            pkg = self._GetPackageInstance(package)
            pkg.LocalUpdate()
    def _LocalUpdateArgs(self, package_name):
        self._DepsArgs(package_name)

    def LocalUninstall(self, package_name):
        for package in self._PackageSet(package_name):
            pkg = self._GetPackageInstance(package)
            pkg.LocalUninstall()
    def _LocalUninstallArgs(self, package_name):
        self._DepsArgs(package_name)

    def _GetPackageInstance(self, package_name):
        JarvisManager.GetInstance().DisableFancyPrint()
        module_name = f"{package_name}_setup"
        class_name = ToCamelCase(module_name)
        module = __import__(f"jarvis_repos.builtin.deps.{module_name}", fromlist=[class_name])
        klass = getattr(module, class_name)
        return klass(self.config, self.jarvis_env)