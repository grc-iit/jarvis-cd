"""Production invariants for the JARVIS-CD release workflow."""

from __future__ import annotations

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text(
    encoding="utf-8"
)


def test_external_actions_are_immutable_commit_pins() -> None:
    """Release jobs must not execute mutable third-party action tags."""
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", WORKFLOW, flags=re.MULTILINE)
    assert uses
    for action in uses:
        if action.startswith("./"):
            continue
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), action


def test_immutable_release_preflight_gates_artifact_build() -> None:
    """Release work starts only after repository immutability is enforced."""
    preflight_index = WORKFLOW.index("  release-preflight:")
    build_index = WORKFLOW.index("  build:")
    assert preflight_index < build_index
    assert 'gh api "repos/$REPOSITORY/immutable-releases" --jq .enabled' in WORKFLOW
    build_block = WORKFLOW[build_index : WORKFLOW.index("  release:")]
    assert "    needs: release-preflight" in build_block


def test_release_resume_is_exact_byte_and_fail_closed() -> None:
    """Reruns may resume exact releases but cannot overwrite or accept drift."""
    assert "load_and_verify_assets true" in WORKFLOW
    assert WORKFLOW.count("load_and_verify_assets false") >= 3
    assert "cmp --silent" in WORKFLOW
    assert "release asset digest mismatch" in WORKFLOW
    assert "unexpected release asset" in WORKFLOW
    assert "missing release asset" in WORKFLOW
    assert "--clobber" not in WORKFLOW


def test_final_release_requires_exact_tag_attestation_and_immutability() -> None:
    """A completed release is pinned, attested, byte-exact, and immutable."""
    assert WORKFLOW.count('test "$(resolve_remote_tag)" = "$GITHUB_SHA"') >= 3
    assert "gh attestation verify" in WORKFLOW
    assert "--deny-self-hosted-runners" in WORKFLOW
    assert 'test "$(jq -r .draft "$release_json")" = false' in WORKFLOW
    assert 'test "$(jq -r .immutable "$release_json")" = true' in WORKFLOW
    assert 'gh release verify "$TAG_NAME"' in WORKFLOW
