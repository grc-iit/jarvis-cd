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

    Nodes should never be created directly and are intended only to be used
    within a Pipeline. Use a Pipeline object instead of creating nodes
    directly
    """

    def __init__(self):
        self.jarvis = JarvisManager.get_instance()
        self.type = to_snake_case(self.__class__.__name__)
        self.node_id = None
        """The node dir (e.g., ${JARVIS_ROOT}/builtin/orangefs)"""
        self.pkg_dir = str(
            pathlib.Path(inspect.getfile(self.__class__)).parent.resolve())
        """The directory which stores configuration data"""
        self.config_dir = None
        """The configuration path"""
        self.config_path = None
        """The configuration for the class"""
        self.config = {}
        """Environment variable cache path"""
        self.env_path = None
        """Environment variable dictionary"""
        self.env = {}

    def create(self, node_id, config_dir):
        """
        Create a brand new node in the pipeline

        :param config_dir: The absolute path to a directory which stores
        the configuration data for the node
        :param node_id: A unique identifier for the node within the context
        of a pipeline. Id does not need to be unique across pipelines.
        """

        """The unique id of this node in the pipeline"""
        self.node_id = node_id
        if node_id is None:
            self.node_id = self.type
        """The directory which stores configuration data"""
        self.config_dir = config_dir
        """The configuration path"""
        self.config_path = f"{self.config_dir}/{self.node_id}.yaml"
        """Environment variable cache path"""
        self.env_path = f"{self.config_dir}/env.yaml"
        """Create directories"""
        os.makedirs(self.config_dir, exist_ok=True)
        """Copy the """
        return self

    def load(self, node_id, config_dir):
        self.node_id = node_id
        self.config_dir = config_dir
        self.config_path = f"{self.config_dir}/{self.node_id}.yaml"
        self.env_path = f"{self.config_dir}/env.yaml"
        self.config = YamlFile(self.config_path).load()
        self.env = YamlFile(self.env_path).load()
        return self

    def save(self):
        YamlFile(self.config_path).save(self.config)
        YamlFile(self.env_path).save(self.env)
        return self

    def destroy(self):
        shutil.rmtree(self.config_dir)

    def set_env(self, env):
        """
        Set the current environment for this program

        :param env: The environment dict
        :return:
        """
        self.env = env


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
    def __init__(self):
        super().__init__()
        self.config = []  # List of (node_type, context)
        self.nodes = []  # List of nodes

    def create(self, pipeline_id, config_dir=None):
        """
        Create a pipeline.

        :param pipeline_id: The unique name of the pipeline in jarvis
        :param config_dir: The directory to place the pipeline configuration
        data
        :return:
        """
        self.jarvis.cd(pipeline_id)
        if config_dir is None:
            config_dir = f'{self.jarvis.config_dir}/{pipeline_id}'
        if self.jarvis.pipeline_exists(pipeline_id):
            self.load(pipeline_id, config_dir)
            return self
        super().create(pipeline_id, config_dir)
        self.jarvis.add_pipeline(self.config_dir, self.node_id)
        return self

    def load(self, pipeline_id=None, config_dir=None):
        """
        Load an existing pipeline

        :param pipeline_id: The unique name of the pipeline in jarvis
        :param config_dir: Ignored.
        :return:
        """

        if pipeline_id is None:
            pipeline_id = self.jarvis.cur_pipeline
        config_dir = self.jarvis.get_pipeline_info(pipeline_id)
        super().load(pipeline_id, config_dir)
        for node_type, node_id in self.config:
            node_config_dir = f"{config_dir}/{node_id}"
            node = self.jarvis.construct_node(node_type)
            node.load(node_id, node_config_dir)
            self.nodes.append(node)
        return self

    def save(self):
        super().save()
        for node in self.nodes:
            node.save()
        self.jarvis.save()
        return self

    def destroy(self):
        for node in self.nodes:
            node.destroy()
        super().destroy()
        self.jarvis.remove_pipeline(self.node_id)

    def append(self, node_type, node_id=None, config=None):
        if node_id is None:
            node_id = node_type
        self.config.append([node_type, node_id])
        node = self.jarvis.construct_node(node_type)
        node.create(node_id, f'{self.config_dir}/{node_id}')
        if node is None:
            raise Exception(f'Cloud not find node: {node_type}')
        if isinstance(node, Service) and config is not None:
            node.configure(config)
        self.nodes.append(node)
        return self

    def remove(self, node_id):
        node = self.get_node(node_id)
        node.destroy()
        self.unlink(node_id)
        return self

    def unlink(self, node_id):
        self.nodes = [test_node for test_node in self.nodes
                      if test_node.node_id != node_id]
        self.config = [[test_node_type, test_node_id]
                       for test_node_type, test_node_id in self.config
                       if test_node_id != node_id]
        return self

    def get_node(self, node_id):
        matches = [node for node in self.nodes if node.node_id == node_id]
        if len(matches) == 0:
            return None
        else:
            return matches[0]

    def configure(self, node_id, config=None):
        node = self.get_node(node_id)
        if node is None:
            raise Exception(f'Cloud not find node: {node_id}')
        if isinstance(node, Service):
            node.configure(config)

    def start(self):
        exec = LocalExecInfo()
        env = exec.env
        for node in self.nodes:
            if isinstance(node, Service):
                node.set_env(env.copy())
                node.start()
            if isinstance(node, Interceptor):
                node.set_env(env)
                node.modify_env()

    def stop(self):
        exec = LocalExecInfo()
        env = exec.env
        for node in reversed(self.nodes):
            if isinstance(node, Service):
                node.set_env(env.copy())
                node.stop()

    def clean(self):
        exec = LocalExecInfo()
        env = exec.env
        for node in reversed(self.nodes):
            if isinstance(node, Service):
                node.set_env(env.copy())
                node.clean()

    def status(self):
        exec = LocalExecInfo()
        env = exec.env
        statuses = []
        for node in reversed(self.nodes):
            if isinstance(node, Service):
                node.set_env(env.copy())
                statuses.append(node.status())
        return math.prod(statuses)
