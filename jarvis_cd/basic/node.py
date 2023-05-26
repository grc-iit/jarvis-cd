"""
This module contains abstract base classes which represent the different
node types in Jarvis.
"""

from abc import ABC, abstractmethod
from jarvis_cd.basic.jarvis_manager import JarvisManager
from jarvis_util.util.naming import to_snake_case
from jarvis_util.serialize.yaml_file import YamlFile
from jarvis_util.shell.local_exec import LocalExecInfo
import inspect
import pathlib
import shutil
import math
import os


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
        if self.shared_dir is not None:
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
        self.config_dir = f'{self.jarvis.config_dir}/{relpath}'
        self.private_dir = f'{self.jarvis.private_dir}/{relpath}'
        if self.shared_dir is not None:
            self.shared_dir = f'{self.jarvis.shared_dir}/{relpath}'
        self.config_path = f'{self.config_dir}/{self.node_id}.yaml'
        self.config = YamlFile(self.config_path).load()
        self.env_path = f'{self.config_dir}/env.yaml'
        self.env = YamlFile(self.env_path).load()
        self.sub_nodes = []
        for sub_node_type, sub_node_id in self.config['sub_nodes']:
            sub_node_config_dir = f'{self.config_dir}/{sub_node_id}'
            sub_node = self.jarvis.construct_node(sub_node_type)
            sub_node.load(f'{self.context}.{sub_node_id}')
            self.sub_nodes.append(sub_node)
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
        shutil.rmtree(self.config_dir)

    def append(self, node_type, node_id=None, config=None):
        """
        Create and append a node to the pipeline

        :param node_type: The type of node to create (e.g., OrangeFS)
        :param node_id: Semantic name of the node to create
        :param config: Any parameters the user want to configure in the node
        :return: self
        """
        if node_id is None:
            node_id = node_type
        self.config['sub_nodes'].append([node_type, node_id])
        node = self.jarvis.construct_node(node_type)
        if node is None:
            raise Exception(f'Cloud not find node: {node_type}')
        context = f'{self.context}.{node_id}'
        node.create(context)
        if isinstance(node, Service) and config is not None:
            node.configure(config)
        self.sub_nodes.append(node)
        return self

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
            for test_node_type, test_node_id in self.config
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

    def set_env(self, env):
        """
        Set the current environment for this program

        :param env: The environment dict
        :return:
        """
        self.env = env

    @staticmethod
    def kwargs_to_config(kwargs):
        """
        Convert a kwargs dict to a nested configuration file.

        :param kwargs: A dictionary
        :return:
        """
        config = {}
        for key, val in kwargs.items():
            nesting = key.split('.')
            if len(nesting) == 1:
                config[key] = val
                continue
            config[key] = {}
            cur_config = config[key]
            for key in nesting[1:-1]:
                cur_config[key] = {}
                cur_config = cur_config[key]
            cur_config[nesting[-1]] = val
        return config


class Interceptor(Node):
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


class Service(Node):
    """
    A service is a long-running process. For example, a storage system is
    a service which runs until explicitly stopped.
    """

    @abstractmethod
    def configure(self, config):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param config: The human-readable jarvis YAML configuration for the
        application.
        :return: None
        """
        pass

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
    def default_configure(self):
        return {}

    def configure(self, node_id, config=None):
        """
        Configure a node in the pipeline

        :param node_id: The semantic name of the node to configure
        :param config: Configuration parameters
        :return:
        """
        node = self.get_node(node_id)
        if node is None:
            raise Exception(f'Cloud not find node: {node_id}')
        if isinstance(node, Service):
            node.configure(config)

    def start(self):
        """
        Start the pipeline

        :return: None
        """
        env = LocalExecInfo().env
        for node in self.sub_nodes:
            if isinstance(node, Service):
                node.set_env(env.copy())
                node.start()
            if isinstance(node, Interceptor):
                node.set_env(env)
                node.modify_env()

    def stop(self):
        """
        Stop the pipeline

        :return: None
        """
        env = LocalExecInfo().env
        for node in reversed(self.sub_nodes):
            if isinstance(node, Service):
                node.set_env(env.copy())
                node.stop()

    def clean(self):
        """
        Clean the pipeline

        :return: None
        """
        env = LocalExecInfo().env
        for node in reversed(self.sub_nodes):
            if isinstance(node, Service):
                node.set_env(env.copy())
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
                node.set_env(env.copy())
                statuses.append(node.status())
        return math.prod(statuses)
