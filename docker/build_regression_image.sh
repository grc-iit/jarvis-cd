#!/bin/bash
# Build the #526 regression APPTAINER SIF — this IS the automated
# "installation" for the containerized single_node/distributed pipelines
# (which live in clio-core under jarvis_clio_core/pipelines/ares/).
# Two stages, docker-only:
#   1. `docker build` docker/regression.Dockerfile -> a local OCI image (it
#      bakes spack iowarp(+fuse) and ior@3.3.0, juicefs, redis, THIS
#      jarvis-cd checkout, and clio-core at a pinned ref; its final RUN
#      fails if any required binary is missing — the install-cleanliness
#      gate).
#   2. `apptainer build <sif> docker-daemon://<image>` -> a portable .sif.
#      No Docker Hub round-trip, no apptainer --fakeroot needed at build time.
#
# Source trees:
#   - jarvis-cd enters via the docker build CONTEXT (this repo's root): the
#     image always matches the checkout you run this script from.
#   - clio-core enters via a git fetch inside the Dockerfile at
#     CLIO_REPO_URL @ CLIO_REF. When CLIO_REF is a branch, this script
#     resolves it to a commit SHA first (git ls-remote) so docker's layer
#     cache busts exactly when clio-core pushes — a raw branch-name build
#     arg would silently reuse a stale cached clone. The resolved SHA is
#     echoed: record it, it is the exact clio-core commit baked in.
#
# The SIF is written to the jarvis containers cache so the pipeline YAMLs
# find it by basename (container_image: "iowarp-regression-526-v3"):
#     <jarvis shared_dir>/containers/iowarp-regression-526-v3.sif
# On Ares shared_dir is on /mnt/common (shared FS) -> the SIF is visible on
# every compute node automatically (no per-node copy).
#
# Re-run daily so #526 tracks the latest IOWarp.
#
# Usage (from anywhere, on a host with docker + apptainer):
#   bash docker/build_regression_image.sh
#
# Common overrides (env):
#   IMAGE=iowarp-regression:526-v3          # local docker image tag to build
#   SIF_BASENAME=iowarp-regression-526-v3   # must match YAML container_image
#   SIF_PATH=/abs/path/iowarp-regression-526-v3.sif  # override the SIF location
#   IOWARP_SPEC='iowarp@dev +fuse'          # upstream dev + FUSE (v3: no +redis)
#   IOR_SPEC='ior@3.3.0'                    # pinned; builtin.ior parses 3.3.0 output
#   BASE_IMAGE=iowarp/iowarp-build:latest
#   CLIO_REPO_URL=https://github.com/eDoggo3779/clio-core-fork.git
#   CLIO_REF=jarvis-pipelines-526           # branch (resolved to SHA) or SHA
#   SKIP_DOCKER_BUILD=1                     # reuse an existing local docker image
set -euo pipefail

IMAGE="${IMAGE:-iowarp-regression:526-v3}"
SIF_BASENAME="${SIF_BASENAME:-iowarp-regression-526-v3}"
IOWARP_SPEC="${IOWARP_SPEC:-iowarp@dev +fuse}"
IOR_SPEC="${IOR_SPEC:-ior@3.3.0}"
BASE_IMAGE="${BASE_IMAGE:-iowarp/iowarp-build:latest}"
CLIO_REPO_URL="${CLIO_REPO_URL:-https://github.com/eDoggo3779/clio-core-fork.git}"
CLIO_REF="${CLIO_REF:-jarvis-pipelines-526}"
SKIP_DOCKER_BUILD="${SKIP_DOCKER_BUILD:-0}"

# Build context = the jarvis-cd repo root (parent of docker/), so the
# Dockerfile's `COPY . /opt/jarvis-cd` bakes in this checkout.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "${SCRIPT_DIR}/.." && pwd )"
DOCKERFILE="${SCRIPT_DIR}/regression.Dockerfile"

# ---- resolve the SIF destination (jarvis containers cache) ----------------
# Derive shared_dir from the jarvis config so the SIF lands where the YAMLs
# look for it by basename. Allow a full SIF_PATH override for non-standard
# setups.
if [ -z "${SIF_PATH:-}" ]; then
  SHARED_DIR="$(grep -hE '^[[:space:]]*shared_dir:' "$HOME"/.ppi-jarvis/*.yaml 2>/dev/null \
                | head -1 | sed -E 's/^[[:space:]]*shared_dir:[[:space:]]*//; s/["'"'"']//g')"
  if [ -z "$SHARED_DIR" ]; then
    echo "ERROR: could not read shared_dir from ~/.ppi-jarvis/*.yaml." >&2
    echo "       Run 'jarvis init' first, or pass SIF_PATH=/abs/path.sif." >&2
    exit 1
  fi
  SIF_PATH="$SHARED_DIR/containers/$SIF_BASENAME.sif"
fi
mkdir -p "$(dirname "$SIF_PATH")"

# ---- resolve CLIO_REF branch/tag -> commit SHA (docker cache-bust) --------
# docker caches the clio fetch layer on the ARG *value*; a branch name would
# silently reuse a stale clone after clio-core pushes. Resolving to a SHA
# makes every clio push produce a fresh value (and records provenance). If
# CLIO_REF is already a SHA (or the remote is unreachable), pass it through.
RESOLVED_REF="$(git ls-remote "$CLIO_REPO_URL" "refs/heads/$CLIO_REF" "refs/tags/$CLIO_REF" 2>/dev/null \
                | head -1 | cut -f1)"
if [ -n "$RESOLVED_REF" ]; then
  echo "CLIO_REF '$CLIO_REF' resolved to commit $RESOLVED_REF"
  CLIO_REF="$RESOLVED_REF"
else
  echo "WARNING: could not resolve CLIO_REF '$CLIO_REF' as a branch/tag on" >&2
  echo "         $CLIO_REPO_URL (already a SHA, or remote unreachable);"    >&2
  echo "         passing it through as-is. Note docker may reuse a cached"  >&2
  echo "         clone layer for a previously-seen value."                  >&2
fi

echo "=== #526 regression SIF build ==="
echo "  docker image : $IMAGE"
echo "  IOWARP_SPEC  : $IOWARP_SPEC"
echo "  IOR_SPEC     : $IOR_SPEC"
echo "  BASE_IMAGE   : $BASE_IMAGE"
echo "  CLIO_REPO_URL: $CLIO_REPO_URL"
echo "  CLIO_REF     : $CLIO_REF"
echo "  SIF_PATH     : $SIF_PATH"

# ---- tool presence --------------------------------------------------------
command -v apptainer >/dev/null || {
  echo "ERROR: apptainer not on PATH. Install it (e.g. spack install" >&2
  echo "       apptainer) and 'spack load apptainer' first."          >&2
  exit 1
}

# ---- stage 1: docker build ------------------------------------------------
if [ "$SKIP_DOCKER_BUILD" = "1" ]; then
  echo "=== SKIP_DOCKER_BUILD=1 — reusing existing local image $IMAGE ==="
else
  command -v docker >/dev/null || {
    echo "ERROR: docker not on PATH — it is required (the docker build is" >&2
    echo "       the only supported build path for this image)."           >&2
    exit 1
  }
  echo "=== docker build $IMAGE ==="
  docker build -f "$DOCKERFILE" \
    --build-arg BASE_IMAGE="$BASE_IMAGE" \
    --build-arg IOWARP_SPEC="$IOWARP_SPEC" \
    --build-arg IOR_SPEC="$IOR_SPEC" \
    --build-arg CLIO_REPO_URL="$CLIO_REPO_URL" \
    --build-arg CLIO_REF="$CLIO_REF" \
    -t "$IMAGE" "$PROJECT_ROOT"
  echo "=== docker build OK: $IMAGE ==="
fi

# ---- stage 2: convert the local docker image to a SIF ---------------------
echo "=== apptainer build $SIF_PATH  (docker-daemon://$IMAGE) ==="
apptainer build --force "$SIF_PATH" "docker-daemon://$IMAGE"

echo "=== SIF ready: $SIF_PATH ==="
ls -lh "$SIF_PATH"
echo "The pipeline YAMLs reference this by basename: container_image: \"$SIF_BASENAME\""
