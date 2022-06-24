import os

class GitArgs:
    def ParseGitArgs(self, repo):
        if repo not in self.conf:
            return
        self.branch = self.conf[repo]['branch']
        self.commit = self.conf[repo]['commit']
        self.repo = self.conf[repo]['repo']
        if "name" in self.conf[repo] and self.conf[repo]['name'] is not None:
            self.repo_name = self.conf[repo]['repo_name']
        else:
            self.repo_name = repo

    def GitCloneCommands(self, cmds):
        cmds.append(f'git clone {self.repo}')
        cmds.append(f'cd {self.repo_name}')
        cmds.append(f'git switch {self.branch}')
        if self.commit is not None:
            cmds.append(f'git switch {self.commit}')

    def GitUpdateCommands(self, cmds, repo_path):
        cmds.append(f'cd {repo_path}')
        cmds.append(f'git pull origin {self.branch}')
        cmds.append(f'git switch {self.branch}')
        if self.commit is not None:
            cmds.append(f'git checkout {self.commit}')