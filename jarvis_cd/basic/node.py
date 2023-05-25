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
    """

    def __init__(self, context):
        """
        Initialize application context

        :param context: A dot-separated path indicating where configuration
        data should be stored for the application. E.g., ior.orangefs would
        inidicate we should place data in ${PRIVATE_DIR}/ior/orangefs/ and
        ${SHARED_DIR}/ior/orangefs
        """
        self.type = to_snake_case(self.__class__.__name__)
        self.context = context
        self.jarvis = JarvisManager.get_instance()
        """The node dir (e.g., ${JARVIS_ROOT}/jarvis_cdbuiltin/orangefs)"""
        self.pkg_dir = str(
            pathlib.Path(inspect.getfile(self.__class__)).parent.resolve())
        """The shared directory where data should be placed"""
        self.shared_dir = self.jarvis.get_shared_dir(self.context)
        """The private directory where data should be placed"""
        self.private_dir = self.jarvis.get_private_dir(self.context)
        """The configuration for the class"""
        self.config = None
        """The configuration path"""
        if os.path.exists(self.jarvis.get_shared_dir()):
            self.config_path = f"{self.shared_dir}/{self.id}.yaml"
        elif os.path.exists(self.jarvis.get_private_dir()):
            self.config_path = f"{self.private_dir}/{self.id}.yaml"
        else:
            self.config_path = None
        """Environment variable dictionary"""
        self.env = {}

    def from_file(self):
        self.config = YamlFile(self.config_path).load()
        return self

    def save(self):
        YamlFile(self.config_path).save(self.config)
        return self

    def destroy(self):
        shutil.rmtree(self.shared_dir)
        shutil.rmtree(self.per_node_dir)

    def create(self):
        try:
            os.makedirs(self.shared_dir)
        except:
            pass

        try:
            os.makedirs(self.private_dir)
        except:
            pass

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
    def modify_env(self, env):
        """
        Modify the jarvis environment.

        :param env: The environment dictionary
        :return: None
        """
        pass


class Service(Node):
    """
    A service is a long-running process. For example, a storage system is
    a service which runs until explicitly stopped.
    """

    @abstractmethod
    def configure(self):
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

    The pipeline file is stored as pipeline.yaml
    """
    def __init__(self, context):
        super().__init__(context)
        self.config = []  # List of (node_type, context)
        self.nodes = []  # List of nodes

    def from_file(self):
        super().from_file()
        for node_type, context in self.config:
            self.nodes.append(self.jarvis.load_node(node_type, context))
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

    def configure(self, id, kwargs=None):
        node = self.get_node(id)
        if node is None:
            raise Exception(f'Cloud not find node: {node_type}')
        if isinstance(node, Service):
            node.configure(kwargs)

    def start(self):
        exec = LocalExecInfo()
        env = exec.env
        for node in self.nodes:
            if isinstance(node, Service):
                node.set_env(env.copy())
                node.start()
            if isinstance(node, Interceptor):
                node.modify_env(env)

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
