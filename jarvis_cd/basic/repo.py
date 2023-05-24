"""
This module contains abstract base classes which represent the different
repo types in Jarvis.
"""

from abc import ABC, abstractmethod
from jarvis_cd.basic.jarvis_manager import JarvisManager
from jarvis_util.util.naming import to_snake_case
from jarvis_util.serialization.yaml_file import YamlFile
from jarvis_util.shell.local_exec_info import LocalExecInfo
import inspect
import math


class Repo(ABC):
    """
    Represents a generic Jarvis repo. Includes methods to load configurations
    and to specialize the repo using a context.
    """

    def __init__(self, context):
        """
        Initialize application context

        :param context: A dot-separated path indicating where configuration
        data should be stored for the application. E.g., ior.orangefs would
        inidicate we should place data in ${PER_NODE_DIR}/ior/orangefs/ and
        ${SHARED_DIR}/ior/orangefs
        """
        self.type = to_snake_case(self.__class__.__name__)
        self.context = context
        self.jarvis = JarvisManager.get_instance()
        """The repo dir (e.g., ${JARVIS_ROOT}/jarvis_cdbuiltin/orangefs)"""
        self.pkg_dir = str(
            pathlib.Path(inspect.getfile(self.__class__)).parent.resolve())
        """The shared directory where data should be placed"""
        self.shared_dir = self.jarvis.get_shared_dir(self.context)
        """The private directory where data should be placed"""
        self.per_node_dir = self.jarvis.get_per_node_dir(self.context)
        """The configuration for the class"""
        self.config = None
        """The configuration path"""
        if os.path.exists(self.jarvis.get_shared_dir()):
            self.config_path = f"{self.shared_dir}/{self.id}.yaml"
        elif os.path.exists(self.jarvis.get_per_node_dir()):
            self.config_path = f"{self.per_node_dir}/{self.id}.yaml"
        else:
            self.config_path = None
        """Environment variable dictionary"""
        self.env = {}

    def from_file(self):
        self.config = YamlFile(self.config_path).Load()
        return self

    def save(self):
        YamlFile(self.config_path).save(self.config)
        return self

    def set_env(self, env):
        """
        Set the current environment for this program

        :param env: The environment dict
        :return:
        """
        self.env = env


class Interceptor(Repo):
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


class Service(Repo):
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


class Pipeline(Repo):
    """
    A pipeline connects the different repo types together in a chain.

    The pipeline file is stored as pipeline.yaml
    """
    def __init__(self, context):
        super().__init__(context)
        self.config = []  # List of (repo_name, context)
        self.nodes = [] # List of nodes

    def from_file(self):
        super().from_file()
        for repo_name, context in self.config:
            self.nodes.append(self.jarvis.load_repo(repo_name, context))
        return self

    def save(self):
        super().save()
        for node in self.nodes:
            node.save()
        return self

    def append(self, repo_name, id=None):
        if id is None:
            id = repo_name
        node_context = f'{self.context}.{id}'
        self.config.append((repo_name, node_context))
        node = self.jarvis.load_repo(repo_name, node_context)
        if node is None:
            raise Exception(f'Cloud not find repo: {repo_name}')
        self.nodes.append(node)

    def remove(self, id):
        self.nodes = [node for node in self.nodes if node.id != id]
        remove_context = f"{self.context}.{id}"
        self.config = [(repo_name, node_context)
                       for repo_name, node_context in self.config
                       if node_context != remove_context]

    def configure(self):
        exec = LocalExecInfo()
        env = exec.env
        for node in self.nodes:
            if isinstance(node, Service):
                node.set_env(env.copy())
                node.configure()

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
