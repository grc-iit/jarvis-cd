# =============================================================================
# regression.Dockerfile — prebuilt image for the #526 CONTAINER pipelines
# (clio-core's single_node.yaml / distributed.yaml with
#  base_deploy_mode: container).
# =============================================================================
#
# The #526 pipelines deploy with APPTAINER, but this Dockerfile is the ONLY
# build path: docker/build_regression_image.sh does the docker build here and
# then converts the result to a portable .sif with
#   apptainer build <sif> docker-daemon://iowarp-regression:526-v3
# (jarvis then starts ONE apptainer instance per node and runs the WHOLE
# pipeline inside it via apptainer exec instance://...). Every package —
# redis, juicefs, ior, clio_* — runs bare-metal inside the container, so a
# FUSE mount made by juicefs/CTE is visible to ior in the same mount
# namespace. Multi-node MPI runs launch mpiexec INSIDE the head instance
# and spawn remote ranks over the instance sshd (container_ssh_port).
#
# THE BUILD IS THE "installs run cleanly" GATE: the final RUN fails the build
# if any required binary is missing (this is how #526 "includes the
# installation").
#
# The image combines TWO source trees:
#   - jarvis-cd: COPY'd from the docker build CONTEXT (this repo's root), so
#     the image always matches the local checkout under validation — no ref
#     pin to bump. Build from the repo root (build_regression_image.sh does).
#   - clio-core: fetched at build time from CLIO_REPO_URL @ CLIO_REF. The
#     fetch-by-ref form below accepts a branch name OR a commit SHA;
#     build_regression_image.sh resolves branches to SHAs first so docker's
#     layer cache busts exactly when clio-core pushes new commits (a raw
#     branch-name ARG would silently reuse a stale cached clone).
#     It supplies (a) the EXTENDED spack recipe — installers/spack adds a
#     `redis` variant the upstream recipe lacks — and (b) the
#     jarvis_clio_core package repo (clio_* pkg_types).
#
# Tailored to the IOWarp build base: it runs as the non-root user `iowarp`,
# ships spack at /home/iowarp/spack, and has NO clio_* built — so system
# installs run as root, the IOWarp build runs as `iowarp` (it owns spack),
# and the runtime user is root (the jarvis compose entrypoint uses /root).
#
# WHY register clio-core's recipe: clio-core owns the iowarp recipe used by
# this image (namespace `iowarp` overrides the builtin), so recipe changes
# ship with the clio branch instead of waiting on a spack upstream. v3
# builds `iowarp@dev +fuse` — the v2-only `+redis` variant (it gated
# clio_redis_bench, which v3 does not ship) is gone from the v3 recipe, so
# a `+redis` spec would fail to concretize. `ior@3.3.0` is installed in the
# same spack invocation so it links against the same MPI stack whose
# `mpiexec` the view puts on PATH (an apt-linked ior would be launched by a
# different MPI — classic version-mismatch breakage). No `+hdf5`: the
# regression sweeps use posix/mpiio.

ARG BASE_IMAGE=iowarp/iowarp-build:latest
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
# IOWarp spec — built with clio-core's recipe (see header). `@dev` is
# upstream dev; +fuse builds clio_cte_fuse. (v3: no +redis — the variant
# left the recipe along with clio_redis_bench.)
ARG IOWARP_SPEC=iowarp@dev +fuse
# IOR pinned: builtin.ior's log parser was written against 3.3.0 output.
ARG IOR_SPEC=ior@3.3.0
ARG JUICEFS_VERSION=1.2.3
# clio-core source: URL + ref (branch or SHA; SHA preferred — see header).
ARG CLIO_REPO_URL=https://github.com/eDoggo3779/clio-core-fork.git
ARG CLIO_REF=jarvis-pipelines-526
ARG SPACK_SETUP=/home/iowarp/spack/share/spack/setup-env.sh
ARG SPACK_USER=iowarp

# 1) System deps as ROOT (the base defaults to USER iowarp -> apt is denied).
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        fuse3 libfuse3-3 \
        openmpi-bin libopenmpi-dev \
        redis-server redis-tools \
        git curl ca-certificates \
        python3 python3-pip python3-venv \
        openssh-server openssh-client \
    && rm -rf /var/lib/apt/lists/*

# 2) JuiceFS — official single static binary (to a root-owned PATH dir).
RUN curl -fsSL "https://github.com/juicedata/juicefs/releases/download/v${JUICEFS_VERSION}/juicefs-${JUICEFS_VERSION}-linux-amd64.tar.gz" \
      | tar -xz -C /usr/local/bin juicefs \
    && juicefs version

# 3) clio-core at the pinned ref (shallow fetch-by-ref: works for branch
#    names AND commit SHAs, unlike `git clone --branch`), plus a spack
#    "view" dir the iowarp user can populate and root can read at runtime.
#    Only the recipe + jarvis packages are needed from the tree;
#    `spack install` fetches the `@dev` SOURCE from upstream GitHub.
RUN git init -q /opt/clio-core \
    && git -C /opt/clio-core fetch -q --depth 1 ${CLIO_REPO_URL} ${CLIO_REF} \
    && git -C /opt/clio-core checkout -q FETCH_HEAD \
    && mkdir -p /opt/iowarp-view && chown ${SPACK_USER}:${SPACK_USER} /opt/iowarp-view

# 4) IOWarp (+FUSE) and IOR via spack, as the user that OWNS spack. Register
#    clio-core's recipe FIRST so `iowarp@dev +fuse` resolves to clio's
#    recipe (its namespace `iowarp` overrides the base image's builtin
#    iowarp pkg). ior is installed in the SAME invocation so it links
#    against the same MPI whose mpiexec ends up on the view PATH.
#
#    The base image's site-scope packages.yaml has external stubs for cmake,
#    python, openmpi, hdf5, and boost that lack concrete versions — spack
#    0.22+ rejects them during concretization. Override them in the user
#    scope (user scope wins over site/system) so spack builds from source
#    instead.
USER ${SPACK_USER}
RUN mkdir -p ~/.spack && cat > ~/.spack/packages.yaml <<'EOF'
packages:
  cmake:
    buildable: true
    externals: []
  python:
    buildable: true
    externals: []
  openmpi:
    buildable: true
    externals: []
  hdf5:
    buildable: true
    externals: []
  boost:
    buildable: true
    externals: []
EOF
RUN . "${SPACK_SETUP}" \
    && spack repo add /opt/clio-core/installers/spack \
    && spack install --fail-fast ${IOWARP_SPEC} ${IOR_SPEC} \
    && spack view --dependencies yes symlink -i /opt/iowarp-view \
         ${IOWARP_SPEC} ${IOR_SPEC}

# 5) Back to root: put the view on PATH, install jarvis-cd from the build
#    context (the local checkout — kept LATE so jarvis-only edits do not
#    rebuild the spack layer), register the clio jarvis repo, prepare sshd.
#    Runtime user stays root (compose /root).
USER root
ENV PATH=/opt/iowarp-view/bin:${PATH}
ENV LD_LIBRARY_PATH=/opt/iowarp-view/lib:/opt/iowarp-view/lib64:${LD_LIBRARY_PATH}

COPY . /opt/jarvis-cd
RUN { pip3 install -e /opt/jarvis-cd \
      || pip3 install --break-system-packages -e /opt/jarvis-cd; } \
    && jarvis init \
    && jarvis repo add /opt/clio-core/jarvis_clio_core \
    && jarvis repo list

RUN mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh

# 5b) Multi-node MPI over the instance sshd needs two image-side fixes:
#     - Remote prted spawn: sshd child sessions get the compiled-in default
#       PATH (the image ENV does not survive an sshd login), so the MPI
#       launch chain must resolve from /usr/local/bin. Spack binaries are
#       rpath'd, so bare symlinks suffice. The [ -e ] guard tolerates
#       OMPI4 (orted) vs OMPI5 (prted) daemon naming.
#     - First-contact host keys: the in-instance `ssh -p <port>` hop to a
#       peer instance would fail interactive host-key verification
#       (instance keys != node keys; no port entries in known_hosts). We
#       PREPEND the Host * block to the system-wide /etc/ssh/ssh_config, not
#       only a drop-in: the base image's ssh_config may lack an
#       `Include ssh_config.d/*.conf` line, so the drop-in alone was silently
#       ignored on Ares (ssh is first-match-wins, so top-of-file always wins).
RUN for b in mpiexec mpirun prted orted orterun ior; do \
        [ -e "/opt/iowarp-view/bin/$b" ] \
            && ln -sf "/opt/iowarp-view/bin/$b" "/usr/local/bin/$b"; \
    done; \
    mkdir -p /etc/ssh/ssh_config.d \
    && printf 'Host *\n    StrictHostKeyChecking no\n    UserKnownHostsFile /dev/null\n    LogLevel ERROR\n' \
         > /etc/ssh/ssh_config.d/instance.conf \
    && touch /etc/ssh/ssh_config \
    && { printf 'Host *\n    StrictHostKeyChecking no\n    UserKnownHostsFile /dev/null\n    LogLevel ERROR\n\n'; \
         cat /etc/ssh/ssh_config; } > /etc/ssh/ssh_config.new \
    && mv /etc/ssh/ssh_config.new /etc/ssh/ssh_config

# 6) GATE — fail the build if a REQUIRED binary is missing. v3 list: ior
#    replaces fio; clio_cte_bench and clio_redis_bench left the project
#    (the redis experiment now uses builtin.redis-benchmark).
RUN for b in clio_run clio_cte_fuse \
             juicefs ior redis-server redis-cli redis-benchmark mpiexec jarvis; do \
        command -v "$b" >/dev/null || { echo "MISSING REQUIRED BINARY: $b"; exit 1; }; \
    done \
    && echo "iowarp-regression image: required binaries present"
