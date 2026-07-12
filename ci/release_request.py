"""Validate a short-lived operator request for an immutable release."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Sequence
from typing import Any, cast

REQUEST_SCHEMA = "jarvis.release.request.v1"
MAX_REQUEST_BYTES = 4_096
REQUEST_KEYS = {
    "commit",
    "immutable_releases_enabled",
    "repository",
    "schema_version",
    "tag",
    "verified_at_epoch",
}
STABLE_TAG_PATTERN = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class ReleaseRequestError(ValueError):
    """Raised when an operator release request is invalid."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting duplicate member names."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseRequestError(
                f"release request contains duplicate field: {key}"
            )
        result[key] = value
    return result


def validate_release_request(
    raw: str,
    *,
    repository: str,
    tag: str,
    commit: str,
    now_epoch: int,
    max_age_seconds: int,
) -> dict[str, Any]:
    """Return an exact release request after validating every field."""
    if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ReleaseRequestError("release request exceeds its byte limit")
    if STABLE_TAG_PATTERN.fullmatch(tag) is None:
        raise ReleaseRequestError("release tag must be a stable vX.Y.Z tag")
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise ReleaseRequestError("release commit must be a lowercase 40-byte hex SHA")
    if max_age_seconds <= 0:
        raise ReleaseRequestError("maximum request age must be positive")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object)
    except ReleaseRequestError:
        raise
    except json.JSONDecodeError as exc:
        raise ReleaseRequestError("release request is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ReleaseRequestError("release request must be a JSON object")
    record = cast(dict[str, Any], document)
    if set(record) != REQUEST_KEYS:
        raise ReleaseRequestError("release request field set is not exact")
    if record["schema_version"] != REQUEST_SCHEMA:
        raise ReleaseRequestError("release request schema is not supported")
    if record["repository"] != repository:
        raise ReleaseRequestError("release request repository does not match")
    if record["tag"] != tag:
        raise ReleaseRequestError("release request tag does not match")
    if record["commit"] != commit:
        raise ReleaseRequestError("release request commit does not match")
    if record["immutable_releases_enabled"] is not True:
        raise ReleaseRequestError("immutable releases were not observed as enabled")
    verified_at = record["verified_at_epoch"]
    if type(verified_at) is not int:
        raise ReleaseRequestError("release request time must be an integer epoch")
    age = now_epoch - verified_at
    if age < 0:
        raise ReleaseRequestError("release request time is in the future")
    if age > max_age_seconds:
        raise ReleaseRequestError("release request has expired")
    return record


def canonical_release_request(
    raw: str,
    *,
    repository: str,
    tag: str,
    commit: str,
    now_epoch: int,
    max_age_seconds: int,
) -> str:
    """Validate and return a canonical single-line release request."""
    record = validate_release_request(
        raw,
        repository=repository,
        tag=tag,
        commit=commit,
        now_epoch=now_epoch,
        max_age_seconds=max_age_seconds,
    )
    return json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--max-age-seconds", required=True, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate the standard-input request and print its canonical form."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    raw = sys.stdin.read(MAX_REQUEST_BYTES + 1)
    try:
        canonical = canonical_release_request(
            raw,
            repository=arguments.repository,
            tag=arguments.tag,
            commit=arguments.commit,
            now_epoch=int(time.time()),
            max_age_seconds=arguments.max_age_seconds,
        )
    except ReleaseRequestError as exc:
        parser.error(str(exc))
    print(canonical)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
