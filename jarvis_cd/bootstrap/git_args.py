import os

class GitArgs:
    def ParseGitArgs(self, repo):
        if repo not in self.conf:
            return
        self.branch = self.conf[repo]['branch']
        self.commit = self.conf[repo]['commit']
        self.repo = self.conf[repo]['repo']
        if "name" in self.conf[repo] and self.conf[repo]['name'] is not None:
            self.repo_name = self.conf[repo]['name']
        else:
            self.repo_name = repo
        if "path" in self.conf[repo] and self.conf[repo]['path'] is not None:
            self.repo_path = self.conf[repo]['path']

    def GitCloneCommands(self, cmds):
        cmds.append(f'mkdir -p {self.repo_path}')
        cmds.append(f'cd {self.repo_path}/..')
        cmds.append(f'git clone {self.repo}')
        cmds.append(f'cd {self.repo_path}')
        cmds.append(f'git switch {self.branch}')
        if self.commit is not None:
            cmds.append(f'git switch {self.commit}')

    def GitUpdateCommands(self, cmds, repo_path):
        cmds.append(f'cd {repo_path}')
        cmds.append(f'git pull origin {self.branch}')
        cmds.append(f'git switch {self.branch}')
        if self.commit is not None:
            cmds.append(f'git checkout {self.commit}')