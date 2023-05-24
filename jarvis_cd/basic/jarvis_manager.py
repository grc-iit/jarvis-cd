"""
This module represents the JarvisCD Manager singleton. It stores an index
of all relevant paths needed by most jarvis repos.
"""

import pathlib
import os
import sys
from jarvis_util.serialization.yaml_file import YamlFile
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
            instance_ = JarvisManager()
        return JarvisManager.instance_

    def __init__(self):
        self.jarvis_root = str(
            pathlib.Path(__file__).parent.parent.parent.resolve())
        self.jarvis_conf_path = os.path.join(self.jarvis_root,
                                             'config', 'jarvis_conf.yaml')
        self.jarvis_conf = None
        if os.path.exists(self.jarvis_conf):
            self.jarvis_conf = YamlFile(self.jarvis_conf).load()
        self.per_node_dir = self.jarvis_conf['PER_NODE_DIR']
        self.shared_dir = self.jarvis_conf['SHARED_DIR']
        self.resource_graph_path = os.path.join(self.jarvis_root,
                                                'config',
                                                'resource_graph.yaml')

    def get_per_node_dir(self, context=''):
        depths = context.split('.')
        return os.path.join(self.per_node_dir, *depths)

    def get_shared_dir(self, context=''):
        depths = context.split('.')
        return os.path.join(self.shared_dir, *depths)

    def add_repos(self, *paths):
        pass

    def load_repo(self, repo_name, context):
        """
        :param repo_name: The name of the repo to load (snake case).
        :param context: A dot-separated identifier indicating the location

        :return:
        """
        load_class('')
