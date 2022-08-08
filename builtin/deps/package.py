from jarvis_cd.installer.git_node import GitNode, GitOps
from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.shell.copy_node import CopyNode
from jarvis_cd.fs.mkdir_node import MkdirNode
from jarvis_cd.fs.fs import ChownFS
from jarvis_cd.launcher.launcher import Launcher
from jarvis_cd.jarvis_manager import JarvisManager
from jarvis_cd.introspect.check_command import CheckCommandNode
from jarvis_cd.util.naming import ToCamelCase
from jarvis_cd.exception import Error,ErrorCode
import os

class Deps(Launcher):
    def _Scaffold(self):
        #Check if jarvis already installed
        if 'JARVIS_ROOT' in os.environ:
            if os.path.exists(os.environ['JARVIS_ROOT']):
                self.config['jarvis_cd']['path'] = os.environ['JARVIS_ROOT']

        #Check if spack already installed
        node = CheckCommandNode('spack').Run()
        if node.Exists():
            self.config['spack']['path'] = os.environ['SPACK_ROOT']

        #Check if scsrepo already installed
        if 'SCS_REPO' in os.environ:
            if os.path.exists(os.environ['SCS_REPO']):
                self.config['scs_repo']['path'] = os.environ['SCS_REPO']

    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.jarvis_shared = self.config['JARVIS_PATHS']['shared']
        self.jarvis_per_node = self.config['JARVIS_PATHS']['per_node']
        if self.jarvis_shared is not None:
            self.jarvis_root = self.jarvis_shared
            self.jarvis_hosts = None
        elif self.jarvis_per_node is not None:
            self.jarvis_root = self.jarvis_per_node
            self.jarvis_hosts = self.all_hosts
        else:
            raise Error(ErrorCode.JARVIS_ROOT_NOT_CONFIGURED).format()
        self.jarvis_shared_pkg_dir = os.path.join(self.jarvis_shared, 'jarvis_instances')
        self.jarvis_per_node_pkg_dir = os.path.join(self.jarvis_per_node, 'jarvis_instances')
        self.config['jarvis_cd']['path'] = self.jarvis_root

    def _DepsArgs(self, parser):
        parser.add_argument('package_name', metavar='pkg', type=str, help="name of package to install")

    def Install(self, package_name, do_python, sudo):
        if package_name == 'jarvis' or package_name == 'all':
            jarvis_conf = os.path.join(self.jarvis_root, 'jarvis_conf.yaml')

            #Create the jarvis shared directory
            MkdirNode(self.jarvis_shared_pkg_dir, sudo=sudo).Run()
            if sudo:
                ChownFS(self.jarvis_shared_pkg_dir).Run()

            #Create the jarvis per-node directory
            MkdirNode(self.jarvis_per_node_pkg_dir, hosts=self.all_hosts, sudo=sudo).Run()
            if sudo:
                ChownFS(self.jarvis_per_node_pkg_dir, hosts=self.all_hosts).Run()

            if 'HOSTS' in self.config and isinstance(self.config['HOSTS'], str):
                hostfile = self.config['HOSTS']
            else:
                hostfile = None
            GitNode(**self.config['jarvis_cd'], method=GitOps.CLONE, hosts=self.jarvis_hosts).Run()
            files = [
                jarvis_conf,
                hostfile
            ]
            CopyNode(files, self.shared_dir, hosts=self.jarvis_hosts).Run()
            cmds = [
                f"cd {self.jarvis_root}",
                f"chmod +x {self.jarvis_root}/dependencies.sh",
                f"{self.jarvis_root}/dependencies.sh" if not do_python else f"DO_PYTHON=1 {jarvis_root}/dependencies.sh",
                f"source ~/.bashrc",
                f"python3 -m pip install -e . --user -r requirements.txt",
                f"./bin/jarvis deps local-install {package_name}"
            ]
            ExecNode(cmds, hosts=self.jarvis_hosts, shell=True).Run()
            exit()
        else:
            ExecNode(f"jarvis deps local-install {package_name} -C {self.jarvis_root}", hosts=self.jarvis_hosts).Run()
    def _InstallArgs(self, parser):
        self._DepsArgs(parser)
        parser.add_argument('--do_python', action='store_true', help='Whether or not to install python as dependency')
        parser.add_argument('--sudo', action='store_true', help='Whether or not to use sudo when creating jarvis directories')

    def Update(self, package_name):
        ExecNode(f"jarvis deps local-update {package_name} -C {self.jarvis_root}", hosts=self.jarvis_hosts).Run()
    def _UpdateArgs(self, parser):
        self._DepsArgs(parser)

    def Uninstall(self, package_name):
        ExecNode(f"jarvis deps local-uninstall {package_name} -C {self.jarvis_root}", hosts=self.jarvis_hosts).Run()
    def _UninstallArgs(self, parser):
        self._DepsArgs(parser)

    def _PackageSet(self, package_name):
        if package_name != 'all':
            return [package_name]
        else:
            return ['jarvis', 'spack', 'scs_repo']

    def LocalInstall(self, package_name):
        for package in self._PackageSet(package_name):
            pkg = self._GetPackageInstance(package)
            pkg.LocalInstall()
    def _LocalInstallArgs(self, parser):
        self._DepsArgs(parser)

    def LocalUpdate(self, package_name):
        for package in self._PackageSet(package_name):
            pkg = self._GetPackageInstance(package)
            pkg.LocalUpdate()
    def _LocalUpdateArgs(self, parser):
        self._DepsArgs(parser)

    def LocalUninstall(self, package_name):
        for package in self._PackageSet(package_name).reverse():
            pkg = self._GetPackageInstance(package)
            pkg.LocalUninstall()
    def _LocalUninstallArgs(self, parser):
        self._DepsArgs(parser)

    def _GetPackageInstance(self, package_name):
        JarvisManager.GetInstance().DisableFancyPrint()
        module_name = f"{package_name}_setup"
        class_name = ToCamelCase(module_name)
        module = __import__(f"jarvis_repos.builtin.deps.{module_name}", fromlist=[class_name])
        klass = getattr(module, class_name)
        return klass(self.config, self.jarvis_env, self.jarvis_root, self.jarvis_shared, self.jarvis_per_node)