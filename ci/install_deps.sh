#!/bin/bash
set -e

sudo apt-get update -y
sudo apt-get install -y mpich openssh-server openssh-client

# Set up passwordless SSH to localhost for tests
ssh-keygen -t rsa -N "" -f ~/.ssh/id_rsa -q
cat ~/.ssh/id_rsa.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# Start SSH daemon
sudo service ssh start

# Add localhost to known_hosts so SSH doesn't prompt
ssh-keyscan -H localhost >> ~/.ssh/known_hosts
