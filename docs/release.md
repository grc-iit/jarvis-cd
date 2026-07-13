# Release procedure

JARVIS-CD releases are built only from a version tag whose commit is already on
`main`. The release workflow builds the wheel and source distribution, tests an
isolated installed wheel, publishes checksums and runtime requirements, attests
every artifact, and verifies the published immutable release.

Before creating a tag, an administrator must verify the repository control that
the Actions token cannot inspect:

```bash
test "$(gh api \
  -H 'Accept: application/vnd.github+json' \
  -H 'X-GitHub-Api-Version: 2026-03-10' \
  repos/grc-iit/jarvis-cd/immutable-releases \
  --jq .enabled)" = true
```

The immutable-release endpoint requires repository administration read access;
the ephemeral `GITHUB_TOKEN` cannot be granted that permission. The operator must
therefore publish a short-lived, exact release-request variable after this check.
The workflow accepts only a request bound to the repository, intended tag, exact
current `main` commit, observed setting, and verification time. It rejects future
records, records older than one hour, unknown fields, and reuse for a different
tag or commit. Repository variables are an audit transport, not an administrator
boundary. Publication is separately gated by the protected `immutable-release`
environment. An explicitly listed release administrator must still review the
waiting deployment and approve the exact tag and commit. Self-review is allowed
so a designated maintainer can release when a second maintainer is unavailable;
GitHub still records the approval event and administrators cannot bypass the
environment gate.

The tag workflow reads the live environment and tag rulesets before building.
It requires `can_admins_bypass=false`, an explicit release-administrator
reviewer list that includes the initiating maintainer, a `v*` tag-only
deployment policy, admin-only version-tag creation, and no-bypass tag
update/deletion protection. Do not tag if those controls have been removed or
changed. Inspect them directly before proceeding:

```bash
gh api repos/grc-iit/jarvis-cd/environments/immutable-release
gh api \
  'repos/grc-iit/jarvis-cd/environments/immutable-release/deployment-branch-policies?per_page=100'
gh api repos/grc-iit/jarvis-cd/rulesets/18798594
gh api repos/grc-iit/jarvis-cd/rulesets/18834187
```

Then verify that the release commit is the current remote `main`, select the
stable release version explicitly, and push the new tag:

```bash
git fetch origin main --tags
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
# JARVIS-CD derives its package version from the immutable Git tag through
# setuptools-scm, so the release operator must select and review the intended
# version explicitly rather than reading a nonexistent static project.version.
: "${RELEASE_VERSION:?set RELEASE_VERSION to a stable X.Y.Z version, for example 2.0.0}"
version="$RELEASE_VERSION"
uv run python - "$version" <<'PY'
import re
import sys

component = r"(?:0|[1-9][0-9]*)"
if re.fullmatch(rf"{component}\.{component}\.{component}", sys.argv[1]) is None:
    raise SystemExit("release version must be a stable X.Y.Z version")
PY
tag="v${version}"
commit="$(git rev-parse HEAD)"
test -z "$(git tag -l "$tag")"
test -z "$(git ls-remote --tags origin "refs/tags/$tag")"
test "$(gh api \
  -H 'Accept: application/vnd.github+json' \
  -H 'X-GitHub-Api-Version: 2026-03-10' \
  repos/grc-iit/jarvis-cd/immutable-releases \
  --jq .enabled)" = true
request="$(jq -cSn \
  --arg repository grc-iit/jarvis-cd \
  --arg tag "$tag" \
  --arg commit "$commit" \
  --argjson verified_at_epoch "$(date +%s)" \
  '{
    schema_version: "jarvis.release.request.v1",
    repository: $repository,
    tag: $tag,
    commit: $commit,
    immutable_releases_enabled: true,
    verified_at_epoch: $verified_at_epoch
  }')"
gh variable set JARVIS_RELEASE_REQUEST \
  --repo grc-iit/jarvis-cd \
  --body "$request"
test "$(gh variable get JARVIS_RELEASE_REQUEST \
  --repo grc-iit/jarvis-cd)" = "$request"
git tag "$tag" "$commit"
git push origin "refs/tags/$tag"
```

The workflow fails if the tag is not on `main`, artifact metadata differs from
the tag, the remote tag moves, the published release is not immutable, or GitHub
cannot verify the release attestation. After it completes, verify independently:

```bash
gh release verify "v${version}" --repo grc-iit/jarvis-cd
gh release view "v${version}" --repo grc-iit/jarvis-cd \
  --json tagName,targetCommitish,isDraft,isImmutable,assets
```
