"""
This module contains abstract base classes which represent the different
node types in Jarvis.
"""

from abc import ABC, abstractmethod
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


class NodeArgParse(ArgParse):
    def define_options(self):
        self.add_menu()
        self.add_args(self.custom_info['menu'])

    def main_menu(self):
        pass


class Node(ABC):
    """
    Represents a generic Jarvis node. Includes methods to load configurations
    and to specialize the node using a context.
    """

    def __init__(self):
        """
        Initialize paths

        :param requires_shared: Whether this repo requires the shared directory
        """

        self.jarvis = JarvisManager.get_instance()
        self.type = to_snake_case(self.__class__.__name__)
        self.context = None
        self.node_id = None
        """The node dir (e.g., ${JARVIS_ROOT}/builtin/orangefs)"""
        self.pkg_dir = str(
            pathlib.Path(inspect.getfile(self.__class__)).parent.resolve())
        self.config_dir = None
        self.private_dir = None
        self.shared_dir = None
        self.config_path = None
        self.config = None
        self.sub_nodes = None
        self.env_path = None
        self.env = None

    def create(self, context):
        """
        Create a new node and its filesystem data

        :param context: A dot-separated, globally unique identifier for
        this node. Indicates where configuration data is stored.
        :return: self
        """

        self.context = context
        self.node_id = context.split('.')[-1]
        relpath = self.context.replace('.', '/')
        self.config_dir = f'{self.jarvis.config_dir}/{relpath}'
        self.private_dir = f'{self.jarvis.private_dir}/{relpath}'
        if self.jarvis.shared_dir is not None:
            self.shared_dir = f'{self.jarvis.shared_dir}/{relpath}'
        self.config_path = f'{self.config_dir}/{self.node_id}.yaml'
        if os.path.exists(self.config_path):
            self.load(context)
            return self
        self.config = {
            'sub_nodes': []
        }
        self.sub_nodes = []
        self.env_path = f'{self.config_dir}/env.yaml'
        self.env = {}
        os.makedirs(self.config_dir, exist_ok=True)
        if self.shared_dir is not None:
            os.makedirs(self.shared_dir, exist_ok=True)
        self._init()
        return self

    def load(self, context=None):
        """
        Load the configuration of a node from the

        :param context: A dot-separated, globally unique identifier for
        this node. Indicates where configuration data is stored.
        :return: self
        """
        self.context = context
        if self.context is None:
            self.context = self.jarvis.cur_pipeline
        self.node_id = self.context.split('.')[-1]
        relpath = self.context.replace('.', '/')
        self.sub_nodes = []
        self.config_dir = f'{self.jarvis.config_dir}/{relpath}'
        self.private_dir = f'{self.jarvis.private_dir}/{relpath}'
        if self.jarvis.shared_dir is not None:
            self.shared_dir = f'{self.jarvis.shared_dir}/{relpath}'
        self.config_path = f'{self.config_dir}/{self.node_id}.yaml'
        if not os.path.exists(self.config_path):
            return self
        self.config = YamlFile(self.config_path).load()
        self.env_path = f'{self.config_dir}/env.yaml'
        self.env = YamlFile(self.env_path).load()
        for sub_node_type, sub_node_id in self.config['sub_nodes']:
            sub_node = self.jarvis.construct_node(sub_node_type)
            sub_node.load(f'{self.context}.{sub_node_id}')
            self.sub_nodes.append(sub_node)
        self._init()
        return self

    def save(self):
        """
        Save a node and its sub-nodes
        :return: Self
        """
        YamlFile(self.config_path).save(self.config)
        YamlFile(self.env_path).save(self.env)
        for node in self.sub_nodes:
            node.save()
        return self

    def destroy(self):
        """
        Destroy a node and its sub-nodes

        :return: None
        """
        for node in self.sub_nodes:
            node.destroy()
        try:
            shutil.rmtree(self.config_dir)
        except FileNotFoundError:
            pass

    def append(self, node_type, node_id=None, **kwargs):
        """
        Create and append a node to the pipeline

        :param node_type: The type of node to create (e.g., OrangeFS)
        :param node_id: Semantic name of the node to create
        :param kwargs: Any parameters the user want to configure in the node
        :return: self
        """
        if node_id is None:
            node_id = self._make_unique_name(node_type)
        self.config['sub_nodes'].append([node_type, node_id])
        node = self.jarvis.construct_node(node_type)
        if node is None:
            raise Exception(f'Cloud not find node: {node_type}')
        context = f'{self.context}.{node_id}'
        node.create(context)
        node.update_env(self.env)
        node.configure(**kwargs)
        self.sub_nodes.append(node)
        return self

    def _make_unique_name(self, node_type):
        if self.get_node(node_type) is None:
            return node_type
        count = 1
        while True:
            new_name = f'{node_type}{count}'
            if self.get_node(new_name) is not None:
                count += 1
            return new_name

    def remove(self, node_id):
        """
        Remove a node from the pipeline & delete its contents

        :param node_id: The name of the node to remove
        :return: self
        """
        node = self.get_node(node_id)
        node.destroy()
        self.unlink(node_id)
        return self

    def unlink(self, node_id):
        """
        Remove a node from the pipeline, but keep its contents in case
        it gets added back.

        :param node_id: The name of the node to remove
        :return: self
        """
        self.sub_nodes = [test_node for test_node in self.sub_nodes
                          if test_node.node_id != node_id]
        self.config['sub_nodes'] = [
            [test_node_type, test_node_id]
            for test_node_type, test_node_id in self.config['sub_nodes']
            if test_node_id != node_id]
        return self

    def get_node(self, node_id):
        """
        Get a node in the pipeline.

        :param node_id: The node id to find
        :return: A node
        """
        matches = [node for node in self.sub_nodes if node.node_id == node_id]
        if len(matches) == 0:
            return None
        else:
            return matches[0]

    def view_nodes(self):
        print(self.to_string_pretty())

    def update_env(self, env):
        """
        Update the current environment for this program

        :param env: The environment dict
        :return:
        """
        env.update(self.env)
        self.env = env

    def build_env(self, env_track_dict=None):
        """
        Build the environment variable cache for this node.

        :param env_track_dict: a dict of booleans. Boolean indicates whether
        to track the environment variable, which are the keys of the dict.
        :return: self
        """
        exec_info = LocalExecInfo()
        self.env = exec_info.basic_env
        self.track_env(env_track_dict)
        return self

    def track_env(self, env_track_dict=None):
        """
        Add and remove cached environment variables.

        :param env_track_dict: a dict of booleans. Boolean indicates whether
        to track the environment variable, which are the keys of the dict.
        :return: self
        """
        if env_track_dict is None:
            return
        for key, val in env_track_dict.items():
            if val:
                if key in os.environ:
                    self.env[key] = os.getenv(key)
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

    def prepend_path(self, env_var, path):
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
        info = [f'{space}{self.type} with name {self.node_id}']
        for sub_node in self.sub_nodes:
            info += sub_node.to_string_list_pretty(depth + 2)
        return info

    @abstractmethod
    def _init(self):
        """
        Initialize variables global to the project.
        Called after load() and create()

        :return:
        """
        pass


class SimpleNode(Node):
    """
    A SimpleNode represents a single program. A pipeline is not a SimpleNode
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
            }
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


class Interceptor(SimpleNode):
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


class Service(SimpleNode):
    """
    A long-running service.
    """
    @abstractmethod
    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary nodes.

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


class Pipeline(Node):
    """
    A pipeline connects the different node types together in a chain.
    """
    def _init(self):
        pass

    def configure(self, node_id, **kwargs):
        """
        Configure a node in the pipeline

        :param node_id: The semantic name of the node to configure
        :param config: Configuration parameters
        :return:
        """
        node = self.get_node(node_id)
        if node is None:
            raise Exception(f'Cloud not find node: {node_id}')
        node.update_env(self.env)
        node.configure(**kwargs)

    def run(self):
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
        for node in self.sub_nodes:
            if isinstance(node, Service):
                node.update_env(env)
                node.start()
            if isinstance(node, Interceptor):
                node.update_env(env)
                node.modify_env()

    def stop(self):
        """
        Stop the pipeline

        :return: None
        """
        env = self.env.copy()
        for node in reversed(self.sub_nodes):
            if isinstance(node, Service):
                node.update_env(env)
                node.stop()

    def clean(self):
        """
        Clean the pipeline

        :return: None
        """
        env = LocalExecInfo().env
        for node in reversed(self.sub_nodes):
            if isinstance(node, Service):
                node.update_env(env.copy())
                node.clean()

    def status(self):
        """
        Get the status of the pipeline

        :return: None
        """
        env = LocalExecInfo().env
        statuses = []
        for node in reversed(self.sub_nodes):
            if isinstance(node, Service):
                node.update_env(env.copy())
                statuses.append(node.status())
        return math.prod(statuses)
