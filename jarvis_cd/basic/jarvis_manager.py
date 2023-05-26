"""
This module represents the JarvisCD Manager singleton. It stores an index
of all relevant paths needed by most jarvis repos.
"""

import pathlib
import os
from jarvis_util.serialize.yaml_file import YamlFile
from jarvis_util.util.import_mod import load_class
from jarvis_util.util.naming import to_camel_case


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
        self.jarvis_conf_path = os.path.join(self.jarvis_root,
                                             'config',
                                             'jarvis_config.yaml')
        self.config_dir = None
        self.private_dir = None
        self.shared_dir = None
        self.cur_pipeline = None
        self.jarvis_conf = None
        self.repos = []
        if os.path.exists(self.jarvis_conf_path):
            self.load()
        self.resource_graph_path = os.path.join(self.jarvis_root,
                                                'config',
                                                'resource_graph.yaml')

    def create(self, config_dir, private_dir, shared_dir=None):
        self.jarvis_conf = {
            'CONFIG_DIR': config_dir,
            'PRIVATE_DIR': private_dir,
            'SHARED_DIR': shared_dir,
            'CUR_PIPELINE': None,
            'REPOS': [],
        }
        self.add_repo(f'{self.jarvis_root}/builtin')
        os.makedirs(f'{self.jarvis_root}/config', exist_ok=True)
        self.save()

    def save(self):
        """
        Save the jarvis conf to the config/jarvis_config.yaml

        :return: None
        """
        self.jarvis_conf['CUR_PIPELINE'] = self.cur_pipeline
        self.jarvis_conf['REPOS'] = self.repos
        YamlFile(self.jarvis_conf_path).save(self.jarvis_conf)

    def load(self):
        """
        Load the jarvis conf from the config/jarvis_config.yaml

        :return:
        """
        self.jarvis_conf = YamlFile(self.jarvis_conf_path).load()
        self.cur_pipeline = self.jarvis_conf['CUR_PIPELINE']
        self.repos = self.jarvis_conf['REPOS']
        self.config_dir = self.jarvis_conf['CONFIG_DIR']
        self.private_dir = self.jarvis_conf['PRIVATE_DIR']
        self.shared_dir = self.jarvis_conf['SHARED_DIR']

    def cd(self, pipeline_id):
        """
        Make jarvis focus on the pipeline with id ID.
        This pipeline will be used for subsequent operations:
            jarvis [start/stop/clean/stop/destroy/configure]

        :param pipeline_id: The id of the pipeline to focus on
        :return:
        """
        self.cur_pipeline = pipeline_id

    def add_repo(self, path):
        """
        Induct a repo into the jarvis managers repo search variable.
        Does not create data on the filesystem.

        :param path: The path to the repo to induct. The basename of the
        repo is assumed to be its repo name. E.g., for /home/hi/myrepo,
        my repo would be the basename.
        :return:
        """

        repo_name = os.path.basename(path)
        self.repos.insert(0, {
            'path': path,
            'name': repo_name
        })

    def create_node(self, node_cls, node_type):
        """
        Creates the skeleton of a node within the primary repo.

        :param node_type: The name of the node to create
        :param node_cls: The type of node to create (Service, Application, etc.)
        :return:
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
        :return:
        """

        main_repo = self.get_repo(repo_name)
        if main_repo is None:
            raise Exception(f'Could not find repo: {repo_name}')
        self.repos = [repo for repo in self.repos if repo_name != repo['name']]
        self.repos.insert(0, main_repo)

    def remove_repo(self, repo_name):
        """
        Remove a repo from consideration. Does not destroy data.

        :param repo_name:
        :return:
        """
        self.repos = [repo for repo in self.repos if repo_name != repo['name']]

    def list_repos(self):
        for repo in self.repos:
            print(f'{repo["name"]}: {repo["path"]}')

    def list_repo(self, repo_name):
        """
        List all of the nodes in a repo

        :param repo_name: The repo to list
        :return:
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
