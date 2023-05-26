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
import math


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
        self.id = None
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

    def create(self, id, config_dir):
        """
        Create a brand new node in the pipeline

        :param config_dir: The absolute path to a directory which stores
        the configuration data for the node
        :param id: A unique identifier for the node within the context
        of a pipeline. Id does not need to be unique across pipelines.
        """

        """The unique id of this node in the pipeline"""
        self.id = id
        if id is None:
            self.id = self.type
        """The directory which stores configuration data"""
        self.config_dir = config_dir
        """The configuration path"""
        self.config_path = f"{self.config_dir}/{self.id}.yaml"
        """Create directories"""
        os.makedirs(self.config_dir, exist_ok=True)
        """Copy the """
        return self

    def load(self, id, config_dir):
        self.id = id
        self.config_path = f"{self.config_dir}/{self.id}.yaml"
        self.config = YamlFile(self.config_path).load()
        self.env = YamlFile(self.env_path).load(self.env)
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
        self.env_path = f'{self.per_node_dir}'

    def create(self, id, config_dir=None):
        if config_dir is None:
            config_dir = self.jarvis.config_dir
        super().create(id, config_dir)

    def load(self, id=None, config_dir=None):
        if id is None:
            id = self.jarvis.cur_pipeline
        if config_dir is None:
            config_dir = self.jarvis.get_pipeline_info(id)
        super().load(id, config_dir)
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
        self.jarvis.pipelines.append(self.context)
        self.jarvis.save()
        return self

    def destroy(self):
        for node in self.nodes:
            node.destroy()
        super().destroy()
        self.jarvis.pipelines.remove(context)
        self.jarvis.save()
        return self

    def append(self, node_type, id=None, kwargs=None):
        if id is None:
            id = node_type
        node_context = f'{self.context}.{id}'
        self.config.append((node_type, node_context))
        node = self.jarvis.load_node(node_type, node_context)
        if node is None:
            raise Exception(f'Cloud not find node: {node_type}')
        if isinstance(node, Service) and kwargs is not None:
            node.configure(kwargs)
        self.nodes.append(node)

    def remove(self, id):
        node = self.get_node(id)
        node.destroy()
        self.unlink(id)

    def unlink(self, id):
        self.nodes = [node for node in self.nodes if node.id != id]
        remove_context = f"{self.context}.{id}"
        self.config = [(node_type, node_context)
                       for node_type, node_context in self.config
                       if node_context != remove_context]

    def get_node(self, id):
        context = f"{self.context}.{id}"
        matches = [node for node in self.nodes if node.context == context]
        if len(matches) == 0:
            return None
        else:
            return matches[0]

    def configure(self, id, config=None):
        node = self.get_node(id)
        if node is None:
            raise Exception(f'Cloud not find node: {node_type}')
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
        for node in self.nodes.reverse():
            if isinstance(node, Service):
                node.set_env(env.copy())
                node.stop()

    def clean(self):
        exec = LocalExecInfo()
        env = exec.env
        for node in self.nodes.reverse():
            if isinstance(node, Service):
                node.set_env(env.copy())
                node.clean()

    def status(self):
        exec = LocalExecInfo()
        env = exec.env
        statuses = []
        for node in self.nodes.reverse():
            if isinstance(node, Service):
                node.set_env(env.copy())
                statuses.append(node.status())
        return math.prod(statuses)
