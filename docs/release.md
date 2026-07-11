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

Then verify that the release commit is the current remote `main`, that the tag
matches the project version, and push the new tag:

```bash
git fetch origin main --tags
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
version="$(uv run python -c \
  'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
test ! "$(git tag -l "v${version}")"
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
