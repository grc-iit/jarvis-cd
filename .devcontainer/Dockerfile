FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# -----------------------------------------------------------------------
# Core system tools + Python + MPI + SSH
# -----------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev python3-venv \
        build-essential git curl wget ca-certificates gnupg \
        openssh-client openssh-server \
        openmpi-bin libopenmpi-dev \
        gcc g++ make \
        uidmap slirp4netns fuse-overlayfs \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# -----------------------------------------------------------------------
# Docker CE  (full daemon — works when container is run --privileged)
# -----------------------------------------------------------------------
RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg \
    && echo \
        "deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
        https://download.docker.com/linux/ubuntu noble stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------
# Podman  (rootless-capable via uidmap + fuse-overlayfs above)
# -----------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        podman \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------
# Apptainer
# -----------------------------------------------------------------------
ARG APPTAINER_VERSION=1.4.1
RUN wget -qO /tmp/apptainer.deb \
        "https://github.com/apptainer/apptainer/releases/download/v${APPTAINER_VERSION}/apptainer_${APPTAINER_VERSION}_amd64.deb" \
    && apt-get install -y /tmp/apptainer.deb \
    && rm /tmp/apptainer.deb

# -----------------------------------------------------------------------
# SSH server setup (required by jarvis multi-node tests)
# -----------------------------------------------------------------------
RUN mkdir -p /var/run/sshd \
    && ssh-keygen -A \
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/'       /etc/ssh/sshd_config \
    && sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && printf "StrictHostKeyChecking no\nUserKnownHostsFile /dev/null\n" \
       >> /etc/ssh/ssh_config

# -----------------------------------------------------------------------
# Python project dependencies (installed at image build time for speed;
# the repo itself is bind-mounted at runtime so "pip install -e ." is run
# via postCreateCommand instead).
# -----------------------------------------------------------------------
RUN pip3 install --no-cache-dir --break-system-packages \
        pyyaml pandas podman-compose pytest

# Default shell
CMD ["/bin/bash"]
