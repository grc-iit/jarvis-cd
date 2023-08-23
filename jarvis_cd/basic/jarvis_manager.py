"""
This module represents the JarvisCD Manager singleton. It stores an index
of all relevant paths needed by most jarvis repos.
"""

import pathlib
import os
from jarvis_util.serialize.yaml_file import YamlFile
from jarvis_util.util.import_mod import load_class
from jarvis_util.util.naming import to_camel_case
from jarvis_util.util.expand_env import expand_env
from jarvis_util.util.hostfile import Hostfile
from jarvis_util.introspect.system_info import ResourceGraph
from jarvis_util.shell.pssh_exec import PsshExecInfo
import getpass


class JarvisManager:
    """
    This class stores relevant paths and loads global configurations for
    internal use by jarvis repos.
    """
    instance_ = None

    @staticmethod
    def get_instance():
        if JarvisManager.instance_ is None:
            JarvisManager.instance_ = JarvisManager()
        return JarvisManager.instance_

    def __init__(self):
        self.jarvis_root = str(
            pathlib.Path(__file__).parent.parent.parent.resolve())
        # The current user
        self.user = getpass.getuser()
        # Where Jarvis stores pipeline data (per-user)
        self.config_dir = None
        # Where Jarvis stores data locally to a node (per-user)
        self.private_dir = None
        # Where Jarvis stores data in a shared directory (per-user)
        self.shared_dir = None
        # The current pipeline (per-user)
        self.cur_pipeline = None
        # The path to the global jarvis configuration (root user)
        self.jarvis_conf_path = os.path.join(self.jarvis_root,
                                             'config',
                                             'jarvis_config.yaml')
        # The Jarvis configuration (per-user)
        self.jarvis_conf = None
        #  The path to the jarvis resource graph (global across users)
        self.resource_graph_path = os.path.join(self.jarvis_root,
                                                'config',
                                                'resource_graph.yaml')
        # The Jarvis resource graph (global across users)
        self.resource_graph = None
        self.hostfile = None
        self.repos = []
        self.load()

    def create(self, config_dir, private_dir, shared_dir=None):
        """
        Create a new root jarvis config under config/$USER/jarvis_config.yaml

        :param config_dir: the directory where jarvis stores pipeline
        metadata
        :param private_dir: a directory which is shared on all nodes, but
        stores data privately to the node
        :param shared_dir: a directory which is shared on all nodes, where
        all nodes have the same view of the data
        :return: None
        """
        self.config_dir = expand_env(config_dir)
        self.private_dir = expand_env(private_dir)
        self.shared_dir = expand_env(shared_dir)
        self.jarvis_conf = {
            # Global parameters
            'CONFIG_DIR': config_dir,
            'PRIVATE_DIR': private_dir,
            'SHARED_DIR': shared_dir,
            'REPOS': [],

            # Per-user parameters
            'HOSTFILE': None,
            'CUR_PIPELINE': None,
        }
        self.add_repo(f'{self.jarvis_root}/builtin')
        self.resource_graph = ResourceGraph()
        self.hostfile = Hostfile()
        os.makedirs(f'{self.jarvis_root}/config', exist_ok=True)
        self.save()

    def save(self):
        """
        Save the jarvis config to the config/ares.yaml

        :return: None
        """
        # Update jarvis conf
        self.jarvis_conf['CUR_PIPELINE'] = self.cur_pipeline
        self.jarvis_conf['REPOS'] = self.repos
        self.jarvis_conf['HOSTFILE'] = self.hostfile.path
        # Save global resource graph
        self.resource_graph.save(self.resource_graph_path)
        # Save global and per-user conf
        YamlFile(self.jarvis_conf_path).save(self.jarvis_conf)

    def load(self):
        """
        Load the jarvis config from the config/ares.yaml

        :return: None
        """
        if not os.path.exists(self.jarvis_conf_path):
            return
        self.jarvis_conf = {}
        # Read global jarvis conf
        self.jarvis_conf.update(YamlFile(self.jarvis_conf_path).load())
        self.repos = self.jarvis_conf['REPOS']
        self.config_dir = expand_env(self.jarvis_conf["CONFIG_DIR"])
        os.makedirs(f'{self.config_dir}', exist_ok=True)
        self.private_dir = expand_env(self.jarvis_conf["PRIVATE_DIR"])
        os.makedirs(f'{self.private_dir}', exist_ok=True)
        if self.jarvis_conf['SHARED_DIR'] is not None:
            self.shared_dir = expand_env(self.jarvis_conf["SHARED_DIR"])
            os.makedirs(f'{self.shared_dir}', exist_ok=True)
        # Read global resource graph
        if os.path.exists(self.resource_graph_path):
            self.resource_graph = ResourceGraph().load(self.resource_graph_path)
        else:
            self.resource_graph = ResourceGraph()
        self.cur_pipeline = self.jarvis_conf['CUR_PIPELINE']
        self.hostfile = Hostfile(hostfile=self.jarvis_conf['HOSTFILE'])

    def set_hostfile(self, path):
        """
        Set the hostfile and re-configure all existing jarvis pipelines

        :return: None
        """
        self.hostfile = Hostfile(hostfile=path)

    def bootstrap_from(self, machine):
        """
        Bootstrap jarvis for a particular machine

        :param machine: The machine config to copy
        :return: None
        """
        config_path = f'{self.jarvis_root}/builtin/config/{machine}.yaml'
        if os.path.exists(config_path):
            config = expand_env(YamlFile(config_path).load())
            new_config_path = f'{self.jarvis_root}/config/jarvis_config.yaml'
            YamlFile(new_config_path).save(config)

        rg_path = f'{self.jarvis_root}/builtin/resource_graph/{machine}.yaml'
        if os.path.exists(rg_path):
            self.resource_graph = ResourceGraph().load(rg_path)
            new_rg_path = f'{self.jarvis_root}/config/resource_graph.yaml'
            self.resource_graph.save(new_rg_path)

    def bootstrap_list(self):
        """
        List machines we can bootstrap with no additional configuration

        :return: None
        """
        configs = os.listdir(f'{self.jarvis_root}/builtin/config')
        for config in configs:
            print(config)

    def print_config(self):
        print(self.config_dir)
        print(self.shared_dir)
        print(self.private_dir)

    def resource_graph_init(self):
        """
        Create an empty resource graph

        :return: None
        """
        self.resource_graph = ResourceGraph()

    def resource_graph_build(self):
        """
        Introspect the system and construct a resource graph.

        :return: None
        """
        self.resource_graph = ResourceGraph()
        self.resource_graph.build(
            PsshExecInfo(hostfile=self.hostfile))

    def list_pipelines(self):
        """
        Get a list of all created pipelines

        :return: List of pipelines
        """
        return os.listdir(self.config_dir)

    def cd(self, pipeline_id):
        """
        Make jarvis focus on the pipeline with id ID.
        This pipeline will be used for subsequent operations:
            jarvis [start/stop/clean/stop/destroy/configure]

        :param pipeline_id: The id of the pipeline to focus on
        :return: None
        """
        self.cur_pipeline = pipeline_id

    def add_repo(self, path):
        """
        Induct a repo into the jarvis managers repo search variable.
        Does not create data on the filesystem.

        :param path: The path to the repo to induct. The basename of the
        repo is assumed to be its repo name. E.g., for /home/hi/myrepo,
        my repo would be the basename.
        :return: None
        """

        repo_name = os.path.basename(path)
        for repo in self.repos:
            if repo['name'] == repo_name:
                repo['path'] = path
                return
        self.repos.insert(0, {
            'path': path,
            'name': repo_name
        })

    def create_node(self, node_cls, node_type):
        """
        Creates the skeleton of a node within the primary repo.

        :param node_type: The name of the node to create
        :param node_cls: The type of node to create (Service, Application, etc.)
        :return: None
        """
        # load the template data
        tmpl_dir = f'{self.jarvis_root}/jarvis_cd/template'
        with open(f'{tmpl_dir}/{node_cls}_templ.py',
                  encoding='utf-8') as fp:
            text = fp.read()

        # Replace MyRepo with the node name
        text = text.replace('MyRepo', to_camel_case(node_type))

        # Write the specialized data
        repo_name = self.repos[0]['name']
        repo_dir = self.repos[0]['path']
        node_path = f'{repo_dir}/{repo_name}/{node_type}'
        os.makedirs(node_path, exist_ok=True)
        with open(f'{node_path}/node.py', 'w',
                  encoding='utf-8') as fp:
            fp.write(text)

    def get_repo(self, repo_name):
        """
        Get the repo information associated with the repo name

        :param repo_name: The repo to get info for
        :return: A dictionary containing the name of the repo and the
        path to the repo
        """

        matches = [repo for repo in self.repos if repo_name == repo['name']]
        if len(matches) == 0:
            return None
        return matches[0]

    def promote_repo(self, repo_name):
        """
        Make all subsequent jarvis append operations track this repo first.

        :param repo_name: The repo to prioritize
        :return: None
        """

        main_repo = self.get_repo(repo_name)
        if main_repo is None:
            raise Exception(f'Could not find repo: {repo_name}')
        self.repos = [repo for repo in self.repos if repo_name != repo['name']]
        self.repos.insert(0, main_repo)

    def remove_repo(self, repo_name):
        """
        Remove a repo from consideration. Does not destroy data.

        :param repo_name: the name of the repo to remove
        :return: None
        """
        self.repos = [repo for repo in self.repos if repo_name != repo['name']]

    def list_repos(self):
        """
        Print all repos in jarvis

        :return: None
        """
        for repo in self.repos:
            print(f'{repo["name"]}: {repo["path"]}')

    def list_repo(self, repo_name):
        """
        List all of the nodes in a repo

        :param repo_name: The repo to list
        :return: None
        """
        repo = self.get_repo(repo_name)
        node_types = os.listdir(repo['path'])
        for node_type in node_types:
            print(node_type)

    def construct_node(self, node_type):
        """
        Construct a node by searching repos for the node type

        :param node_type: The type of node to load (snake case).
        :return: A object of type "node_type"
        """
        for repo in self.repos:
            cls = load_class(f'{repo["name"]}.{node_type}.node',
                             self.repos[0]['path'],
                             to_camel_case(node_type))
            if cls is None:
                continue
            return cls()
