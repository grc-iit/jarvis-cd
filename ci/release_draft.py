"""Select one exact GitHub draft release from a paginated API response."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any, cast

MAX_RELEASE_LIST_BYTES = 64 * 1024 * 1024


class ReleaseDraftError(ValueError):
    """Raised when a draft release cannot be resumed safely."""


class ReleaseDraftNotFound(ReleaseDraftError):
    """Raised when no draft exists for the requested tag."""


def _release_pages(raw: str) -> list[list[dict[str, Any]]]:
    """Parse the exact nested array emitted by ``gh api --paginate --slurp``."""
    if len(raw.encode("utf-8")) > MAX_RELEASE_LIST_BYTES:
        raise ReleaseDraftError("release list exceeds its byte limit")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseDraftError("release list is not valid JSON") from exc
    if not isinstance(document, list) or not document:
        raise ReleaseDraftError("release list must contain at least one API page")

    pages: list[list[dict[str, Any]]] = []
    for page in document:
        if not isinstance(page, list):
            raise ReleaseDraftError("release list pages must be JSON arrays")
        releases: list[dict[str, Any]] = []
        for release in page:
            if not isinstance(release, dict):
                raise ReleaseDraftError("release list entries must be JSON objects")
            releases.append(cast(dict[str, Any], release))
        pages.append(releases)
    return pages


def select_exact_draft(
    raw: str,
    *,
    tag: str,
    title: str,
    body: str,
    author: str,
) -> dict[str, Any]:
    """Return the only draft for ``tag`` after validating immutable metadata."""
    candidates = [
        release
        for page in _release_pages(raw)
        for release in page
        if release.get("tag_name") == tag
    ]
    if not candidates:
        raise ReleaseDraftNotFound(f"no draft release exists for tag {tag}")
    if len(candidates) != 1:
        raise ReleaseDraftError(f"multiple releases exist for tag {tag}")

    release = candidates[0]
    expected = {
        "tag_name": tag,
        "name": title,
        "body": body,
        "draft": True,
        "prerelease": False,
        "immutable": False,
    }
    for field, value in expected.items():
        if release.get(field) != value:
            raise ReleaseDraftError(f"draft release {field} does not match")
    release_author = release.get("author")
    if not isinstance(release_author, dict) or release_author.get("login") != author:
        raise ReleaseDraftError("draft release author does not match")
    release_id = release.get("id")
    if type(release_id) is not int or release_id <= 0:
        raise ReleaseDraftError("draft release id is not a positive integer")
    if not isinstance(release.get("assets"), list):
        raise ReleaseDraftError("draft release assets must be a JSON array")
    return release


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--author", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Select and print the exact draft, returning 3 when it does not exist."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    raw = sys.stdin.read(MAX_RELEASE_LIST_BYTES + 1)
    try:
        release = select_exact_draft(
            raw,
            tag=arguments.tag,
            title=arguments.title,
            body=arguments.body,
            author=arguments.author,
        )
    except ReleaseDraftNotFound:
        return 3
    except ReleaseDraftError as exc:
        parser.error(str(exc))
    print(json.dumps(release, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
