#!/bin/bash
# git remote add iowarp https://github.com/iowarp/platform-plugins-interface.git
# git remote add grc https://github.com/grc-iit/jarvis-cd.git
gh pr create --title $1 --body "" --repo=grc-iit/jarvis-cd
gh pr create --title $1 --body "" --repo=iowarp/platform-plugins-interface
