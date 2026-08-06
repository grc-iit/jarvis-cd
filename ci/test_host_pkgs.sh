#!/bin/bash
# Host-prerequisite (`host_pkgs`) check, exercised on a host that does NOT
# have the prerequisite installed.
#
# The example pipeline is a containerized apptainer pipeline: jarvis has to
# shell out to the host's apptainer to build the SIF. This CI host has
# neither spack nor apptainer, which is the whole point -- the run must stop
# at the declared prerequisite with an actionable message, rather than
# wandering into the container build and dying on `apptainer: command not
# found` after it has already created state.
#
# Asserted:
#   1. the run fails (non-zero exit)
#   2. the failure names the missing package and the command that fixes it
#   3. it fails BEFORE the container build starts
#   4. a satisfiable prerequisite does not trip the check (so the test is
#      proving a real check, not a pipeline that always fails)
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="${HOST_PKGS_TEST_DIR:-${HOME}/jarvis-host-pkgs-ci}"
EXAMPLE="${REPO_ROOT}/builtin/pipelines/examples/ior_apptainer_host_pkgs_test.yaml"

rm -rf "${WORK_DIR}"
mkdir -p "${WORK_DIR}/home" "${WORK_DIR}/config" "${WORK_DIR}/private" "${WORK_DIR}/shared"

# Jarvis roots its state at $HOME/.ppi-jarvis, and `jarvis init` rewrites
# that config in place. Point HOME at a throwaway dir so running this
# script on a developer machine cannot repoint their real jarvis install
# at these scratch directories.
export HOME="${WORK_DIR}/home"

echo "=== Initializing jarvis (HOME=${HOME}) ==="
jarvis init "${WORK_DIR}/config" "${WORK_DIR}/private" "${WORK_DIR}/shared"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# Guard the premise: if this host somehow HAS apptainer/spack, the test below
# is not testing what it claims to and must not silently "pass".
if command -v spack >/dev/null 2>&1; then
    fail "spack is present on this host; the missing-prerequisite case cannot be exercised"
fi
if command -v apptainer >/dev/null 2>&1; then
    fail "apptainer is present on this host; the missing-prerequisite case cannot be exercised"
fi

echo
echo "=== Case 1: declared host_pkg (spack apptainer) is NOT installed ==="
OUTPUT_FILE="${WORK_DIR}/run.log"
set +e
jarvis ppl run yaml "${EXAMPLE}" >"${OUTPUT_FILE}" 2>&1
RC=$?
set -e
echo "--- jarvis output (exit ${RC}) ---"
cat "${OUTPUT_FILE}"
echo "--- end output ---"

[ "${RC}" -ne 0 ] || fail "expected a non-zero exit for a missing host package, got 0"

grep -q "Missing 1 required host package" "${OUTPUT_FILE}" \
    || fail "output does not report the missing host package"
grep -q "apptainer (install_method: spack)" "${OUTPUT_FILE}" \
    || fail "output does not name the missing package and its install_method"
grep -q "spack install apptainer" "${OUTPUT_FILE}" \
    || fail "output does not name the command that fixes it"

# Fail-fast: the check must precede the container build, otherwise the
# declaration bought nothing over the raw `command not found`.
if grep -q "ContainerInstaller" "${OUTPUT_FILE}"; then
    fail "container build started despite the missing host prerequisite"
fi
echo "PASS: missing host package stopped the run with an actionable message"

echo
echo "=== Case 2: a satisfiable host_pkg passes the check ==="
# pyyaml is a hard dependency of jarvis, so it is installed by definition
# here. Same code path, opposite outcome -- this is what keeps case 1
# honest.
SAT_YAML="${WORK_DIR}/host_pkgs_satisfied.yaml"
cat >"${SAT_YAML}" <<'YAML'
name: host_pkgs_satisfied
host_pkgs:
  - install_method: pip
    install_query: pyyaml
pkgs: []
YAML

set +e
jarvis ppl run yaml "${SAT_YAML}" >"${WORK_DIR}/sat.log" 2>&1
SAT_RC=$?
set -e
echo "--- jarvis output (exit ${SAT_RC}) ---"
cat "${WORK_DIR}/sat.log"
echo "--- end output ---"

[ "${SAT_RC}" -eq 0 ] || fail "an installed host package should not fail the check (exit ${SAT_RC})"
grep -q "required host package" "${WORK_DIR}/sat.log" \
    && fail "an installed host package was reported missing"

echo "PASS: satisfiable host package cleared the check"
echo
echo "All host_pkgs CI assertions passed."
