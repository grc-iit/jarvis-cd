"""
This module represents the JarvisCD Manager singleton. It stores an index
of all relevant paths needed by most jarvis repos.
"""

import pathlib
import os
import sys
from jarvis_util.serialize.yaml_file import YamlFile
from jarvis_util.util.import_mod import load_class


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
        self.cur_pipeline = None
        self.jarvis_conf = None
        self.pipelines = []
        self.repos = []
        self.private_dir = None
        self.shared_dir = None
        if os.path.exists(self.jarvis_conf_path):
            self.load()
        self.resource_graph_path = os.path.join(self.jarvis_root,
                                                'config',
                                                'resource_graph.yaml')

    def create(self, private_dir=None, shared_dir=None):
        self.jarvis_conf = {
            'PRIVATE_DIR': private_dir,
            'SHARED_DIR': shared_dir,
            'CUR_PIPELINE': None,
            'PIPELINES': [],
            'REPOS': [{
                'path': f'{self.jarvis_root}/builtin',
                'name': 'builtin'
            }],
        }
        os.makedirs(f"{self.jarvis_root}/config", exist_ok=True)
        self.save()

    def save(self):
        """
        Save the jarvis conf to the config/jarvis_config.yaml

        :return: None
        """
        self.jarvis_conf['CUR_PIPELINE'] = self.cur_pipeline
        self.jarvis_conf['PIPELINES'] = self.pipelines
        self.jarvis_conf['REPOS'] = self.repos
        YamlFile(self.jarvis_conf_path).save(self.jarvis_conf)

    def load(self):
        """
        Load the jarvis conf from the config/jarvis_config.yaml

        :return:
        """
        self.jarvis_conf = YamlFile(self.jarvis_conf_path).load()
        self.cur_pipeline = self.jarvis_conf['CUR_PIPELINE']
        self.pipelines = self.jarvis_conf['PIPELINES']
        self.repos = self.jarvis_conf['REPOS']
        self.private_dir = self.jarvis_conf['PRIVATE_DIR']
        self.shared_dir = self.jarvis_conf['SHARED_DIR']

    def get_private_dir(self, context=''):
        """
        Get the private directory for the jarvis context.
        The global private dir is is empty string.

        :param context: A dot-sperated string indicating the directories where
        jarvis stores configuration data.
        :return:
        """
        depths = context.split('.')
        return os.path.join(self.private_dir, *depths)

    def get_shared_dir(self, context=''):
        """
        Get the shared directory for the current jarvis context.

        :param context: A dot-sperated string indicating the directories where
        jarvis stores configuration data.
        :return:
        """
        depths = context.split('.')
        return os.path.join(self.shared_dir, *depths)

    def add_repo(self, path):
        repo_name = os.path.basename(path)
        self.repos.append({
            'path': path,
            'name': repo_name
        })

    def get_repo(self, repo_name):
        matches = [repo for repo in self.repos if repo_name == repo['name']]
        if len(matches) == 0:
            return None
        return matches[0]

    def promote_repo(self, repo_name):
        main_repo = self.get_repo(repo_name)
        if matches is None:
            raise Exception(f'Could not find repo: {repo_name}')
        self.repos = [repo for repo in self.repos if repo_name != repo['name']]
        self.repos.insert(0, main_repo)

    def remove_repo(self, repo_name):
        self.repos = [repo for repo in self.repos if repo_name != repo['name']]

    def list_repos(self):
        for repo in self.repos:
            print(f'{repo["name"]}: {repo["path"]}')

    def list_repo(self, repo_name):
        repo = self.get_repo(repo_name)
        node_types = os.listdir(repo['path'])
        for node_type in node_types:
            print(node_type)

    def get_node(self, node_type, context):
        """
        :param node_type: The type of node to load (snake case).
        :param context: A dot-separated identifier indicating the location
        of

        :return:
        """
        for repo in self.repos:
            cls = load_class(f'{repo["name"]}.{node_type}')
            if cls is None:
                continue
            return cls(context)
