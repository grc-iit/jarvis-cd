"""Production invariants for the JARVIS-CD release workflow."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text(
    encoding="utf-8"
)
COMMIT = "a" * 40
TAG = "v1.2.0"


def _publish_script() -> str:
    workflow = yaml.safe_load(WORKFLOW)
    steps = workflow["jobs"]["release"]["steps"]
    return next(
        step["run"] for step in steps if step.get("name") == "Publish immutable release"
    )


def _bash_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    result = subprocess.run(
        ["wsl.exe", "wslpath", "-a", "-u", str(path).replace("\\", "/")],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _prepare_release_tree(root: Path) -> tuple[Path, dict[str, bytes]]:
    dist = root / "dist"
    state = root / "state"
    assets = state / "assets"
    dist.mkdir(parents=True)
    assets.mkdir(parents=True)
    payloads = {
        "jarvis_cd-1.2.0-py3-none-any.whl": b"wheel\n",
        "jarvis_cd-1.2.0.tar.gz": b"sdist\n",
        "release-metadata.json": b'{"schema_version":"1.0"}\n',
        "runtime-requirements.txt": b"dependency==1.0\n",
    }
    for name, payload in payloads.items():
        (dist / name).write_bytes(payload)
    checksums = "".join(
        f"{hashlib.sha256(payload).hexdigest()} *{name}\n"
        for name, payload in payloads.items()
    ).encode()
    payloads["SHA256SUMS"] = checksums
    (dist / "SHA256SUMS").write_bytes(checksums)
    first_name = "jarvis_cd-1.2.0-py3-none-any.whl"
    shutil.copyfile(dist / first_name, assets / first_name)
    starter = state / "starter"
    starter.mkdir()
    (starter / "jarvis_cd-1.2.0.tar.gz").touch()
    return state, payloads


def _release_shell_stub() -> str:
    return r"""
asset_id() {
  case "$1" in
    SHA256SUMS) printf '1001\n' ;;
    jarvis_cd-1.2.0-py3-none-any.whl) printf '1002\n' ;;
    jarvis_cd-1.2.0.tar.gz) printf '1003\n' ;;
    release-metadata.json) printf '1004\n' ;;
    runtime-requirements.txt) printf '1005\n' ;;
    *) return 1 ;;
  esac
}

emit_release() {
  local draft="$1" immutable="$2" assets_json='[]' file name id digest size
  for file in "$STATE"/assets/*; do
    [ -e "$file" ] || continue
    name="$(basename "$file")"
    id="$(asset_id "$name")"
    digest="sha256:$(sha256sum "$file" | cut -d' ' -f1)"
    size="$(wc -c <"$file")"
    assets_json="$(jq -cn \
      --argjson current "$assets_json" \
      --argjson id "$id" \
      --arg name "$name" \
      --arg digest "$digest" \
      --argjson size "$size" \
      '$current + [{id: $id, name: $name, digest: $digest, state: "uploaded", size: $size}]')"
  done
  for file in "$STATE"/starter/*; do
    [ -e "$file" ] || continue
    name="$(basename "$file")"
    id="$(asset_id "$name")"
    assets_json="$(jq -cn \
      --argjson current "$assets_json" \
      --argjson id "$id" \
      --arg name "$name" \
      '$current + [{id: $id, name: $name, digest: null, state: "starter", size: 0}]')"
  done
  jq -cn \
    --arg tag "$TAG_NAME" \
    --arg body "JARVIS-CD $TAG_NAME immutable release." \
    --argjson draft "$draft" \
    --argjson immutable "$immutable" \
    --argjson assets "$assets_json" '
      {
        id: 123,
        tag_name: $tag,
        name: $tag,
        body: $body,
        draft: $draft,
        prerelease: false,
        immutable: $immutable,
        author: {login: "github-actions[bot]"},
        assets: $assets
      }
    '
}

python() {
  touch "$STATE/python-called"
  [ "$1" = "ci/release_draft.py" ]
  jq -cer \
    --arg tag "$TAG_NAME" \
    --arg body "JARVIS-CD $TAG_NAME immutable release." '
      [.[][] | select(.tag_name == $tag)] as $matches
      | if ($matches | length) == 0 then halt_error(3)
        elif (
          ($matches | length) == 1
          and $matches[0].name == $tag
          and $matches[0].body == $body
          and $matches[0].draft == true
          and $matches[0].prerelease == false
          and $matches[0].immutable == false
          and $matches[0].author.login == "github-actions[bot]"
        ) then $matches[0] else error("unsafe draft fixture") end
    '
}

sleep() {
  [ "$1" = 5 ]
  printf '%s\n' "$1" >>"$STATE/sleep-log"
}

gh() {
  local group="$1" operation="${2:-}" method=GET input='' endpoint='' id name file
  local content_type=false api_version=false accept_json=false
  local draft_field='' latest_field=''
  shift
  if [ "$group" = release ]; then
    shift
    if [ "$operation" = verify ] && [ -f "$STATE/published" ]; then
      return 0
    fi
    if [ "$operation" = create ] && [ -f "$STATE/create-race" ]; then
      printf '%s\n' "$TAG_NAME" >>"$STATE/create-log"
      touch "$STATE/draft-created"
      return 0
    fi
    echo "unexpected gh release operation: $operation" >&2
    return 64
  fi
  [ "$group" = api ] || return 64
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --method) method="$2"; shift 2 ;;
      --input) input="$2"; shift 2 ;;
      -H)
        case "$2" in
          'Content-Type: application/octet-stream') content_type=true ;;
          'X-GitHub-Api-Version: 2022-11-28') api_version=true ;;
          'Accept: application/vnd.github+json') accept_json=true ;;
          'Accept: application/octet-stream') ;;
          *) return 64 ;;
        esac
        shift 2
        ;;
      -F) draft_field="$2"; shift 2 ;;
      -f) latest_field="$2"; shift 2 ;;
      --jq) shift 2 ;;
      --paginate|--slurp) shift ;;
      *) endpoint="$1"; shift ;;
    esac
  done
  case "$endpoint" in
    repos/*/git/ref/tags/*)
      printf '%s\tcommit\n' "$GITHUB_SHA"
      ;;
    repos/*/releases/tags/*)
      if [ ! -f "$STATE/published" ]; then
        echo 'gh: Not Found (HTTP 404)' >&2
        return 1
      fi
      emit_release false true
      ;;
    *'/releases?per_page=100')
      if [ -f "$STATE/fail-pagination" ]; then
        echo 'pagination failed' >&2
        return 1
      fi
      if [ -f "$STATE/create-race" ]; then
        if [ ! -f "$STATE/draft-created" ]; then
          printf '[[]]\n'
          return 0
        fi
        if [ ! -f "$STATE/draft-list-delay-complete" ]; then
          touch "$STATE/draft-list-delay-complete"
          printf '[[]]\n'
          return 0
        fi
      fi
      release="$(emit_release true false)"
      jq -cn --argjson release "$release" '[[$release]]'
      ;;
    repos/*/releases/assets/*)
      id="${endpoint##*/}"
      if [ "$method" = DELETE ]; then
        [ "$api_version" = true ]
        for file in "$STATE"/starter/*; do
          [ -e "$file" ] || continue
          if [ "$(asset_id "$(basename "$file")")" = "$id" ]; then
            printf '%s\n' "$(basename "$file")" >>"$STATE/delete-log"
            rm "$file"
            return 0
          fi
        done
        return 1
      fi
      [ "$method" = GET ]
      for file in "$STATE"/assets/*; do
        [ -e "$file" ] || continue
        if [ "$(asset_id "$(basename "$file")")" = "$id" ]; then
          cat "$file"
          return 0
        fi
      done
      return 1
      ;;
    https://uploads.github.com/*/releases/123/assets?name=*)
      [ "$method" = POST ]
      [ -n "$input" ]
      [ "$content_type" = true ]
      [ "$api_version" = true ]
      name="${endpoint##*name=}"
      cp "$input" "$STATE/assets/$name"
      printf '%s\n' "$name" >>"$STATE/upload-log"
      id="$(asset_id "$name")"
      jq -cn --argjson id "$id" --arg name "$name" \
        '{id: $id, name: $name, state: "uploaded"}'
      ;;
    repos/*/releases/latest)
      [ -f "$STATE/published" ]
      printf '%s\n' "$TAG_NAME"
      ;;
    repos/*/releases/123)
      [ "$method" = PATCH ]
      [ "$accept_json" = true ]
      [ "$api_version" = true ]
      [ "$draft_field" = draft=false ]
      [ "$latest_field" = make_latest=true ]
      touch "$STATE/published"
      printf 'publish\n' >>"$STATE/publish-log"
      emit_release false true
      ;;
    *)
      echo "unexpected gh api endpoint: $endpoint" >&2
      return 64
      ;;
  esac
}
"""


def _run_publish_script(root: Path) -> subprocess.CompletedProcess[str]:
    root_for_bash = _bash_path(root)
    script = f"""
set -euo pipefail
export GITHUB_SHA={COMMIT}
export REPOSITORY=grc-iit/jarvis-cd
export TAG_NAME={TAG}
TEST_ROOT='{root_for_bash}'
STATE="$TEST_ROOT/state"
cd "$TEST_ROOT"
{_release_shell_stub()}
{_publish_script()}
"""
    result = subprocess.run(
        ["bash"],
        input=script.encode(),
        capture_output=True,
    )
    return subprocess.CompletedProcess(
        args=result.args,
        returncode=result.returncode,
        stdout=result.stdout.decode(errors="replace"),
        stderr=result.stderr.decode(errors="replace"),
    )


def test_external_actions_are_immutable_commit_pins() -> None:
    """Release jobs must not execute mutable third-party action tags."""
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", WORKFLOW, flags=re.MULTILINE)
    assert uses
    for action in uses:
        if action.startswith("./"):
            continue
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), action


def test_exact_release_request_gates_artifact_build() -> None:
    """Release work starts only after a fresh exact operator request."""
    preflight_index = WORKFLOW.index("  release-preflight:")
    build_index = WORKFLOW.index("  build:")
    assert preflight_index < build_index
    assert "JARVIS_RELEASE_REQUEST" in WORKFLOW
    assert "python ci/release_request.py" in WORKFLOW
    assert WORKFLOW.count("python ci/release_request.py") == 2
    assert '--repository "$REPOSITORY"' in WORKFLOW
    assert '--tag "$TAG_NAME"' in WORKFLOW
    assert '--commit "$GITHUB_SHA"' in WORKFLOW
    assert "--max-age-seconds 3600" in WORKFLOW
    assert 'gh api "repos/$REPOSITORY/immutable-releases"' not in WORKFLOW
    assert "Verify protected release controls" in WORKFLOW
    assert ".can_admins_bypass == false" in WORKFLOW
    assert ".prevent_self_review == false" in WORKFLOW
    assert '"JaimeCernuda",' in WORKFLOW
    assert '"type": "tag"' in WORKFLOW
    assert "Restrict immutable version tag creation" in WORKFLOW
    assert "tag actor is not an approved release administrator" in WORKFLOW
    assert "collaborators/$release_admin/permission" in WORKFLOW
    assert '.permission == "admin" and .role_name == "admin"' in WORKFLOW
    assert "test/unit/core/test_artifact_spi.py" in WORKFLOW
    assert "test/unit/core/test_progress_spi.py" in WORKFLOW
    assert "test/unit/core/test_pipeline_coverage.py" in WORKFLOW
    assert "test/unit/ci/test_live_ares_gray_scott_probe.py" in WORKFLOW
    for release_contract_test in (
        "test/unit/core/test_execution_cli.py",
        "test/unit/core/test_service_runtime.py",
        "test/unit/core/test_resource_graph_activation.py",
        "test/unit/core/test_paraview_scene_v2.py",
        "test/unit/core/test_paraview_service.py",
    ):
        assert release_contract_test in WORKFLOW
    assert "test/unit/shell/test_local_exec.py" in WORKFLOW
    assert "test/unit/shell/test_mpi_exec.py" in WORKFLOW
    assert "test/unit/util/test_pkg_argparse.py" in WORKFLOW
    assert "'schema_version': 'jarvis.artifact.v1'" in WORKFLOW
    assert "jarvis execution artifacts" in WORKFLOW
    assert "'public_schema': 'jarvis.service-runtime.v2'" in WORKFLOW
    assert "'private_schema': 'jarvis.service-runtime.private.v1'" in WORKFLOW
    assert "resolve-service-runtime-authority" in WORKFLOW
    assert "MAX_HTTP_CONNECTIONS" in WORKFLOW
    assert "sys.path.insert(0, str(distribution_root / 'builtin'))" in WORKFLOW
    assert "bin/jarvis rg builtins | grep -Fx ares" in WORKFLOW
    assert "bin/jarvis rg load-builtin ares +json" in WORKFLOW
    assert "jarvis.resource-graph-builtin.v1" in WORKFLOW
    build_block = WORKFLOW[build_index : WORKFLOW.index("  release:")]
    assert "    needs: release-preflight" in build_block
    assert "release_request" in build_block
    release_block = WORKFLOW[WORKFLOW.index("  release:") :]
    assert "    environment: immutable-release" in release_block
    assert "Revalidate fresh request and current main" in release_block
    assert "refs/remotes/origin/main" in release_block


def test_release_resume_is_exact_byte_and_fail_closed() -> None:
    """Reruns may resume exact releases but cannot overwrite or accept drift."""
    assert "load_and_verify_assets true" in WORKFLOW
    assert WORKFLOW.count("load_and_verify_assets false") >= 3
    assert "gh api --paginate --slurp" in WORKFLOW
    assert "python ci/release_draft.py" in WORKFLOW
    assert "find_existing_release || lookup_status=$?" in WORKFLOW
    assert 'if [ "$lookup_status" -eq 3 ]' in WORKFLOW
    assert "if ! gh api --paginate --slurp" in WORKFLOW
    assert "find_draft_release" in WORKFLOW
    assert "releases/$release_id/assets?name=$encoded_name" in WORKFLOW
    assert '"repos/$REPOSITORY/releases/$release_id"' in WORKFLOW
    assert "gh release upload" not in WORKFLOW
    assert "gh release edit" not in WORKFLOW
    assert 'if [ "$state" = starter ]' in WORKFLOW
    assert '"repos/$REPOSITORY/releases/assets/$id"' in WORKFLOW
    assert "unsafe starter release asset" in WORKFLOW
    assert "cmp --silent" in WORKFLOW
    assert "release asset digest mismatch" in WORKFLOW
    assert "unexpected release asset" in WORKFLOW
    assert "missing release asset" in WORKFLOW
    assert '.author.login == "github-actions[bot]"' in WORKFLOW
    assert ".name == $tag" in WORKFLOW
    assert ".body == $body" in WORKFLOW
    assert ".prerelease == false" in WORKFLOW
    assert '--notes "$release_body"' in WORKFLOW
    assert "--generate-notes" not in WORKFLOW
    assert "--clobber" not in WORKFLOW


def test_new_draft_release_waits_for_bounded_list_consistency() -> None:
    """A newly created draft may take time to appear in the list endpoint."""
    release_block = WORKFLOW[WORKFLOW.index("  release:") :]
    create_index = release_block.index('gh release create "$TAG_NAME"')
    wait_index = release_block.index("wait_for_draft_release", create_index)

    assert "wait_for_draft_release()" in release_block
    assert "for attempt in {1..12}; do" in release_block
    assert "if find_draft_release; then" in release_block
    assert 'else\n                status="$?"' in release_block
    assert 'if [ "$status" -ne 3 ]; then' in release_block
    assert "sleep 5" in release_block
    assert "did not become list-visible after 12 attempts" in release_block
    assert release_block.count('gh release create "$TAG_NAME"') == 1
    assert create_index < wait_index


def test_release_shell_retries_new_draft_visibility_race(tmp_path: Path) -> None:
    """Publishing resumes after a newly created draft becomes list-visible."""
    race_root = tmp_path / "create-race"
    state, _ = _prepare_release_tree(race_root)
    (state / "create-race").touch()

    result = _run_publish_script(race_root)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert (state / "published").is_file()
    assert (state / "create-log").read_text(encoding="utf-8").splitlines() == [TAG]
    assert (state / "sleep-log").read_text(encoding="utf-8").splitlines() == ["5"]


def test_release_shell_resumes_exact_partial_draft_and_fails_closed(
    tmp_path: Path,
) -> None:
    """The executable recovery path is ID-bound, idempotent, and fail-closed."""
    resume_root = tmp_path / "resume"
    state, payloads = _prepare_release_tree(resume_root)

    first = _run_publish_script(resume_root)

    assert first.returncode == 0, f"stdout:\n{first.stdout}\nstderr:\n{first.stderr}"
    assert (state / "published").is_file()
    assert (state / "publish-log").read_text(encoding="utf-8").splitlines() == [
        "publish"
    ]
    assert (state / "delete-log").read_text(encoding="utf-8").splitlines() == [
        "jarvis_cd-1.2.0.tar.gz"
    ]
    assert not any((state / "starter").iterdir())
    uploaded = (state / "upload-log").read_text(encoding="utf-8").splitlines()
    assert sorted(uploaded) == sorted(
        set(payloads) - {"jarvis_cd-1.2.0-py3-none-any.whl"}
    )
    for name, payload in payloads.items():
        assert (state / "assets" / name).read_bytes() == payload

    second = _run_publish_script(resume_root)

    assert second.returncode == 0, f"stdout:\n{second.stdout}\nstderr:\n{second.stderr}"
    assert (state / "publish-log").read_text(encoding="utf-8").splitlines() == [
        "publish"
    ]
    assert (state / "upload-log").read_text(encoding="utf-8").splitlines() == uploaded
    assert (state / "delete-log").read_text(encoding="utf-8").splitlines() == [
        "jarvis_cd-1.2.0.tar.gz"
    ]

    failure_root = tmp_path / "pagination-failure"
    failure_state, _ = _prepare_release_tree(failure_root)
    (failure_state / "fail-pagination").touch()

    failed = _run_publish_script(failure_root)

    assert failed.returncode != 0
    assert not (failure_state / "python-called").exists()
    assert not (failure_state / "published").exists()


def test_final_release_requires_exact_tag_attestation_and_immutability() -> None:
    """A completed release is pinned, attested, byte-exact, and immutable."""
    assert WORKFLOW.count('test "$(resolve_remote_tag)" = "$GITHUB_SHA"') >= 3
    assert "gh attestation verify" in WORKFLOW
    assert "--deny-self-hosted-runners" in WORKFLOW
    assert 'test "$(jq -r .draft "$release_json")" = false' in WORKFLOW
    assert 'test "$(jq -r .immutable "$release_json")" = true' in WORKFLOW
    assert 'gh release verify "$TAG_NAME"' in WORKFLOW
