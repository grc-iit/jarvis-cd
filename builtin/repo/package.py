from jarvis_cd.launcher.launcher import Launcher
from jarvis_cd.fs.link_node import LinkNode
from jarvis_cd.jarvis_manager import JarvisManager
import os

class Repo(Launcher):
    def Add(self, dir=None):
        if dir is None:
            dir = os.getcwd()
        src_repo_path = os.path.join(dir, 'builtin')
        dst_repo_path = JarvisManager.GetInstance().NewLauncherPath(src_repo_path)
        LinkNode(src_repo_path, dst_repo_path, hosts=self.all_hosts).Run()

    def _AddArgs(self, parser):
        parser.add_argument('-D', dest='dir', type=str,
            help='the path to a jarvis repo (contains a \"builtin\" directory).')

    def Remove(self, repo_name):
        repo_path = JarvisManager.GetInstance().GetRepoPath(repo_name)
        os.remove(repo_path)

    def _RemoveArgs(self, parser):
        parser.add_argument('repo_name', type=str,
                            help='the name of the jarvis repo to unregister')
