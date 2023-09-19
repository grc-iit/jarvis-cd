"""
This module contains abstract base classes which represent the different
pkg types in Jarvis.
"""

from abc import ABC, abstractmethod
from jarvis_util.jutil_manager import JutilManager
from jarvis_cd.basic.jarvis_manager import JarvisManager
from jarvis_util.util.naming import to_snake_case
from jarvis_util.serialize.yaml_file import YamlFile
from jarvis_util.shell.local_exec import LocalExecInfo
from jarvis_util.util.argparse import ArgParse
import inspect
import pathlib
import shutil
import math
import os


class PkgArgParse(ArgParse):
    def define_options(self):
        self.add_menu()
        self.add_args(self.custom_info['menu'])

    def main_menu(self):
        pass


class Pkg(ABC):
    """
    Represents a generic Jarvis pkg. Includes methods to load configurations
    and to specialize the pkg using a global_id.
    """

    def __init__(self):
        """
        Initialize paths

        :param pkg_type: The type of this package
        """
        self.jarvis = JarvisManager.get_instance()
        self.jutil = JutilManager.get_instance()
        self.pkg_type = to_snake_case(self.__class__.__name__)
        self.root = None
        self.global_id = None
        self.pkg_id = None
        """The pkg dir (e.g., ${JARVIS_ROOT}/builtin/orangefs)"""
        self.pkg_dir = str(
            pathlib.Path(inspect.getfile(self.__class__)).parent.resolve())
        self.config_dir = None
        self.private_dir = None
        self.shared_dir = None
        self.config_path = None
        self.config = None
        self.sub_pkgs = []
        self.env_path = None
        self.env = None

    def _init_common(self, global_id, root):
        """
        Update paths in this package based on the global_id

        :param global_id: The unique identifier for this package
        :return: None
        """
        if root is None:
            root = self
        self.root = root
        self.global_id = self._get_global_id(global_id)
        id_split = self.global_id.split('.')
        self.pkg_id = id_split[-1]
        relpath = self.global_id.replace('.', '/')
        self.config_dir = f'{self.jarvis.config_dir}/{relpath}'
        self.private_dir = f'{self.jarvis.private_dir}/{relpath}'
        if self.jarvis.shared_dir is not None:
            self.shared_dir = f'{self.jarvis.shared_dir}/{relpath}'
        self.config_path = f'{self.config_dir}/{self.pkg_id}.yaml'
        if len(id_split) > 1:
            self.env_path = None
        else:
            self.env_path = f'{self.config_dir}/env.yaml'

    def _get_global_id(self, global_id):
        if global_id is None:
            global_id = self.jarvis.cur_pipeline
        if global_id is None:
            raise Exception('No pipeline currently selected')
        return global_id

    def create(self, global_id):
        """
        Create a new pkg and its filesystem data

        :param global_id: A dot-separated, globally unique identifier for
        this pkg. Indicates where configuration data is stored.
        :return: self
        """
        self._init_common(global_id, self.root)
        if os.path.exists(self.config_path):
            self.load(global_id, self.root)
            return self
        self.config = {
            'sub_pkgs': []
        }
        self.sub_pkgs = []
        self.env_path = f'{self.config_dir}/env.yaml'
        self.env = {}
        os.makedirs(self.config_dir, exist_ok=True)
        if self.shared_dir is not None:
            os.makedirs(self.shared_dir, exist_ok=True)
        self._init()
        return self

    def load(self, global_id=None, root=None, with_config=True):
        """
        Load the configuration of a pkg from the

        :param global_id: A dot-separated, globally unique identifier for
        this pkg. Indicates where configuration data is stored.
        :param root: The parent
        :param with_config: Whether to load pkg configurations
        :return: self
        """
        self._init_common(global_id, root)
        if not os.path.exists(self.config_path):
            return self
        if not with_config:
            return self
        self.config = YamlFile(self.config_path).load()
        if self.env_path is not None:
            self.env = YamlFile(self.env_path).load()
        else:
            self.env = self.root.env
        for sub_pkg_type, sub_pkg_id in self.config['sub_pkgs']:
            sub_pkg = self.jarvis.construct_pkg(sub_pkg_type)
            sub_pkg.load(f'{self.global_id}.{sub_pkg_id}', self.root)
            self.sub_pkgs.append(sub_pkg)
        self._init()
        return self

    def save(self):
        """
        Save a pkg and its sub-pkgs
        :return: Self
        """
        self.config['pkg_type'] = self.pkg_type
        YamlFile(self.config_path).save(self.config)
        if self.env_path is not None:
            YamlFile(self.env_path).save(self.env)
        for pkg in self.sub_pkgs:
            pkg.save()
        return self

    def clear(self):
        """
        Destroy a pipeline's sub-pkgs

        :return: None
        """
        try:
            for path in os.listdir(self.config_dir):
                if os.path.isdir(path):
                    shutil.rmtree(path)
            os.remove(self.config_path)
        except FileNotFoundError:
            pass

    def destroy(self):
        """
        Destroy a pipeline and its sub-pkgs

        :return: None
        """
        for pkg in self.sub_pkgs:
            pkg.destroy()
        try:
            shutil.rmtree(self.config_dir)
        except FileNotFoundError:
            pass

    def append(self, pkg_type, pkg_id=None, do_configure=True, **kwargs):
        """
        Create and append a pkg to the pipeline

        :param pkg_type: The type of pkg to create (e.g., OrangeFS)
        :param pkg_id: Semantic name of the pkg to create
        :param do_configure: Whether to configure while appending
        :param kwargs: Any parameters the user want to configure in the pkg
        :return: self
        """
        if pkg_id is None:
            pkg_id = self._make_unique_name(pkg_type)
        self.config['sub_pkgs'].append([pkg_type, pkg_id])
        pkg = self.jarvis.construct_pkg(pkg_type)
        if pkg is None:
            raise Exception(f'Cloud not find pkg: {pkg_type}')
        global_id = f'{self.global_id}.{pkg_id}'
        pkg.create(global_id)
        if do_configure:
            pkg.update_env(self.env)
            pkg.configure(**kwargs)
        self.sub_pkgs.append(pkg)
        return self

    def _make_unique_name(self, pkg_type):
        if self.get_pkg(pkg_type) is None:
            return pkg_type
        count = 1
        while True:
            new_name = f'{pkg_type}{count}'
            if self.get_pkg(new_name) is not None:
                count += 1
            return new_name

    def remove(self, pkg_id):
        """
        Remove a pkg from the pipeline & delete its contents

        :param pkg_id: The name of the pkg to remove
        :return: self
        """
        pkg = self.get_pkg(pkg_id)
        pkg.destroy()
        self.unlink(pkg_id)
        return self

    def unlink(self, pkg_id):
        """
        Remove a pkg from the pipeline, but keep its contents in case
        it gets added back.

        :param pkg_id: The name of the pkg to remove
        :return: self
        """
        self.sub_pkgs = [test_pkg for test_pkg in self.sub_pkgs
                          if test_pkg.pkg_id != pkg_id]
        self.config['sub_pkgs'] = [
            [test_pkg_type, test_pkg_id]
            for test_pkg_type, test_pkg_id in self.config['sub_pkgs']
            if test_pkg_id != pkg_id]
        return self

    def get_pkg(self, pkg_id):
        """
        Get a pkg in the pipeline.

        :param pkg_id: The pkg id to find
        :return: A pkg
        """
        matches = [pkg for pkg in self.sub_pkgs if pkg.pkg_id == pkg_id]
        if len(matches) == 0:
            return None
        else:
            return matches[0]

    def view_pkgs(self):
        print(self.to_string_pretty())

    def update_env(self, env):
        """
        Update the current environment for this program

        :param env: The environment dict
        :return:
        """
        env.update(self.env)
        self.env = env

    def track_env(self, env_track_dict=None):
        """
        Add and remove cached environment variables.

        :param env_track_dict: a dict of booleans or strings. Boolean indicates
        whether to track the environment variable, which are the keys
        of the dict. String indicates track the variable and set to this value.
        :return: self
        """
        if env_track_dict is None:
            return
        for key, val in env_track_dict.items():
            if isinstance(val, str):
                self.env[key] = val
                continue
            if val:
                if key in os.environ:
                    self.env[key] = os.getenv(key)
                else:
                    self.env[key] = ''
            else:
                if key in self.env:
                    del self.env[key]
        return self

    def scan_env(self, rescan_list=None):
        """
        Re-scan environment variables.

        :param rescan_list: a list of keys to re-scan.
        :return: self
        """
        if rescan_list is None:
            rescan_list = list(self.env.keys())
        for key in rescan_list:
            self.env[key] = os.getenv(key)
        return self

    def prepend_env(self, env_var, path):
        """
        Prepend a path to the an environment variable, such as LD_PRELOAD.

        :param env_var: The name of the environment variable
        :param path: The path to prepend
        :return:
        """
        if env_var in self.env:
            cur_env = self.env[env_var]
        else:
            cur_env = os.getenv(env_var)

        if cur_env is None or len(cur_env) == 0:
            self.env[env_var] = path
        else:
            self.env[env_var] = f'{path}:{cur_env}'

    def setenv(self, env_var, val):
        """
        Set the Jarvis environment variable

        :param env_var: The environment variable
        :param val: The value of the environment variable
        :return:
        """
        self.env[env_var] = val

    def find_library(self, lib_name, env_vars=None):
        """
        Find the location of a shared object automatically using environment
        variables. If None, will search LD_LIBRARY_PATH.

        :param lib_name: The library to search for. We will search for
        any file matching lib{lib_name}.so and {lib_name}.so.
        :param env_vars: A list of environment variables to search for or
        a string for a single variable.
        :return: string or None
        """
        if env_vars is None:
            env_vars = ['LD_LIBRARY_PATH']
        name_opts = [
            f'{lib_name}.so',
            f'lib{lib_name}.so',
        ]
        for env_var in env_vars:
            if env_var not in self.env:
                continue
            paths = self.env[env_var].split(':')
            for path in paths:
                if not os.path.exists(path):
                    continue
                filenames = os.listdir(path)
                for name_opt in name_opts:
                    if name_opt in filenames:
                        return f'{path}/{name_opt}'
        return None

    def __str__(self):
        return self.to_string_pretty()

    def __repr__(self):
        return self.to_string_pretty()

    def to_string_pretty(self):
        return '\n'.join(self.to_string_list_pretty())

    def to_string_list_pretty(self, depth=0):
        space = ' ' * depth
        info = [f'{space}{self.pkg_type} with name {self.pkg_id}']
        for sub_pkg in self.sub_pkgs:
            info += sub_pkg.to_string_list_pretty(depth + 2)
        return info

    @abstractmethod
    def _init(self):
        """
        Initialize variables global to the project.
        Called after load() and create()

        :return:
        """
        pass


class SimplePkg(Pkg):
    """
    A SimplePkg represents a single program. A pipeline is not a SimplePkg
    because it represents a combination of multiple programs.
    """

    def configure_menu(self):
        """
        Add some common configuration options used across all CLI menus.

        :return:
        """
        menu = self._configure_menu()
        menu += [
            {
                'name': 'sleep',
                'msg': 'How much time to sleep during start (seconds)',
                'type': int,
                'default': 0,
            },
            {
                'name': 'reinit',
                'msg': 'Destroy previous configuration and rebuild',
                'type': bool,
                'default': False
            },
        ]
        return menu

    @abstractmethod
    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.
        For thorough documentation of these parameters, view:
        https://github.com/scs-lab/jarvis-util/wiki/3.-Argument-Parsing

        :return: List(dict)
        """
        return []

    @abstractmethod
    def configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: The human-readable jarvis YAML configuration for the
        application.
        :return: None
        """
        pass

    def update_config(self, kwargs, rebuild=False):
        """
        The kwargs to pack with default values

        :param kwargs: the key-word arguments to fill default values for
        :param rebuild: whether to reinitialize self.config
        from self.conifgure_menu
        :return:
        """
        default_args = ArgParse.default_kwargs(self.configure_menu())
        if not rebuild:
            default_args.update(self.config)
        default_args.update(kwargs)
        self.config = default_args

    @staticmethod
    def copy_template_file(src, dst, replacements=None):
        """
        Copy and configure an application template file
        Template files makr constants using the notation
        ##CONST_NAME##

        :param src: Path to the template
        :param dst: Destination of the template
        :param replacements: A list of 2-tuples. First entry is the name
        of the constant to replace, right is the value to replace it with.
        :return: None
        """
        with open(src, 'r', encoding='utf-8') as fp:
            text = fp.read()
        if replacements is not None:
            for const_name, replace in replacements:
                text = text.replace(f'##{const_name}##', replace)
        with open(dst, 'w', encoding='utf-8') as fp:
            fp.write(text)


class Interceptor(SimplePkg):
    """
    An interceptor is a library which routes function calls to a custom
    function. This typically requires modifications to various environment
    variables, including LD_PRELOAD.
    """

    @abstractmethod
    def modify_env(self):
        """
        Modify the jarvis environment.

        :return: None
        """
        pass


class Service(SimplePkg):
    """
    A long-running service.
    """
    @abstractmethod
    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        pass

    @abstractmethod
    def stop(self):
        """
        Stop a running application. E.g., OrangeFS will terminate the servers,
        clients, and metadata services.

        :return:
        """
        pass

    @abstractmethod
    def clean(self):
        """
        Destroy all data for an application. E.g., OrangeFS will delete all
        metadata and data directories in addition to the orangefs.xml file.

        :return: None
        """
        pass

    @abstractmethod
    def status(self):
        """
        Check whether or not an application is running. E.g., are OrangeFS
        servers running?

        :return: True or false
        """
        pass


class Application(Service):
    """
    An application is a process which will terminate on its own eventually.
    This can be a benchmark program, such as IOR, for example.
    """

    def status(self):
        return True


class Pipeline(Pkg):
    """
    A pipeline connects the different pkg types together in a chain.
    """
    def _init(self):
        pass

    def configure(self, pkg_id, **kwargs):
        """
        Configure a pkg in the pipeline

        :param pkg_id: The semantic name of the pkg to configure
        :param config: Configuration parameters
        :return:
        """
        pkg = self.get_pkg(pkg_id)
        if pkg is None:
            raise Exception(f'Cloud not find pkg: {pkg_id}')
        pkg.update_env(self.env)
        pkg.configure(**kwargs)

    def build_env(self, env_track_dict=None):
        """
        Build the environment variable cache for this pkg.

        :param env_track_dict: a dict of booleans. Boolean indicates whether
        to track the environment variable, which are the keys of the dict.
        :return: self
        """
        exec_info = LocalExecInfo()
        self.env = exec_info.basic_env
        self.track_env(env_track_dict)
        self.update()
        return self

    def update(self):
        """
        Re-run configure on all sub-pkgs.

        :return: self
        """
        for pkg in self.sub_pkgs:
            pkg.env = self.env
            pkg.configure()
        return self

    def run(self):
        """
        Start and stop the pipeline

        :return: None
        """
        self.start()
        self.stop()

    def start(self):
        """
        Start the pipeline.

        NOTE: Start CAN hang for pipelines which spawn
        daemonized processes. This is because input/output is
        too useful to ignore. Python will attempt to close all
        file descriptors when the process exits, so the file descriptor
        used for piping output will be open for as long as the daemon.
        If using start directly, you should launch as background process.

        :return: None
        """
        env = self.env.copy()
        for pkg in self.sub_pkgs:
            if isinstance(pkg, Service):
                pkg.update_env(env)
                pkg.start()
            if isinstance(pkg, Interceptor):
                pkg.update_env(env)
                pkg.modify_env()

    def stop(self):
        """
        Stop the pipeline

        :return: None
        """
        env = self.env.copy()
        for pkg in reversed(self.sub_pkgs):
            if isinstance(pkg, Service):
                pkg.update_env(env)
                pkg.stop()

    def clean(self):
        """
        Clean the pipeline

        :return: None
        """
        env = LocalExecInfo().env
        for pkg in reversed(self.sub_pkgs):
            if isinstance(pkg, Service):
                pkg.update_env(env.copy())
                pkg.clean()

    def status(self):
        """
        Get the status of the pipeline

        :return: None
        """
        env = LocalExecInfo().env
        statuses = []
        for pkg in reversed(self.sub_pkgs):
            if isinstance(pkg, Service):
                pkg.update_env(env.copy())
                statuses.append(pkg.status())
        return math.prod(statuses)
