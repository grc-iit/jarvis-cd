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
the ephemeral `GITHUB_TOKEN` cannot be granted that permission. Do not create or
push the version tag unless this preflight succeeds.

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
test -z "$(git tag -l "v${version}")"
git tag "v${version}" HEAD
git push origin "v${version}"
```

The workflow fails if the tag is not on `main`, artifact metadata differs from
the tag, the remote tag moves, the published release is not immutable, or GitHub
cannot verify the release attestation. After it completes, verify independently:

```bash
gh release verify "v${version}" --repo grc-iit/jarvis-cd
gh release view "v${version}" --repo grc-iit/jarvis-cd \
  --json tagName,targetCommitish,isDraft,isImmutable,assets
```
