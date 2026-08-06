#!/bin/bash
# Build the performance evaluation SIF — the automated installation step for
# the containerized single_node/distributed pipelines, which live in clio-core
# under jarvis_clio_core/pipelines/ares/.
#
# Two stages:
#   1. docker build docker/perf_eval.Dockerfile -> a local OCI image baking
#      spack iowarp(+fuse) + ior, juicefs, redis, this jarvis-cd checkout, and
#      clio-core at CLIO_REF. Its final RUN fails if a required binary is
#      missing.
#   2. apptainer build <sif> docker-daemon://<image> -> a portable .sif. No
#      registry round-trip and no apptainer --fakeroot at build time.
#
# A CLIO_REF branch name is resolved to a commit SHA first (git ls-remote) so
# docker's layer cache busts exactly when clio-core pushes; a raw branch name
# would silently reuse a stale cached clone. The resolved SHA is echoed —
# record it, it is the exact clio-core commit baked into the image.
#
# The SIF lands in the jarvis containers cache so the pipeline YAMLs find it by
# basename (container_image: "iowarp-perf-eval"):
#     <jarvis shared_dir>/containers/iowarp-perf-eval.sif
# On a shared filesystem that makes it visible on every compute node with no
# per-node copy.
#
# Usage:
#   bash docker/build_perf_eval_image.sh          # re-run daily
#   Env overrides: IMAGE, SIF_BASENAME (must match the YAMLs' container_image),
#   SIF_PATH, IOWARP_SPEC, IOR_SPEC, BASE_IMAGE, CLIO_REPO_URL, CLIO_REF,
#   SKIP_DOCKER_BUILD=1 (reuse an existing local docker image).
#
# Output: <SIF_PATH>, printed and listed on success.
set -euo pipefail

IMAGE="${IMAGE:-iowarp-perf-eval:latest}"
SIF_BASENAME="${SIF_BASENAME:-iowarp-perf-eval}"
IOWARP_SPEC="${IOWARP_SPEC:-iowarp@dev +fuse}"
IOR_SPEC="${IOR_SPEC:-ior@3.3.0}"
BASE_IMAGE="${BASE_IMAGE:-iowarp/iowarp-build:latest}"
CLIO_REPO_URL="${CLIO_REPO_URL:-https://github.com/iowarp/clio-core.git}"
CLIO_REF="${CLIO_REF:-dev}"
SKIP_DOCKER_BUILD="${SKIP_DOCKER_BUILD:-0}"

# Build context = the jarvis-cd repo root (parent of docker/), so the
# Dockerfile's `COPY . /opt/jarvis-cd` bakes in this checkout.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "${SCRIPT_DIR}/.." && pwd )"
DOCKERFILE="${SCRIPT_DIR}/perf_eval.Dockerfile"

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

echo "=== performance evaluation SIF build ==="
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
