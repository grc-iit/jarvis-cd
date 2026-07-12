"""Tests for an exact immutable-release operator request."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from ci.release_request import (
    REQUEST_SCHEMA,
    ReleaseRequestError,
    canonical_release_request,
    validate_release_request,
)

REPOSITORY = "grc-iit/jarvis-cd"
TAG = "v2.0.0"
COMMIT = "a" * 40
NOW = 1_800_000_000


def _record() -> dict[str, Any]:
    return {
        "schema_version": REQUEST_SCHEMA,
        "repository": REPOSITORY,
        "tag": TAG,
        "commit": COMMIT,
        "immutable_releases_enabled": True,
        "verified_at_epoch": NOW - 30,
    }


def _validate(
    record: object,
    *,
    repository: str = REPOSITORY,
    tag: str = TAG,
    commit: str = COMMIT,
    now_epoch: int = NOW,
    max_age_seconds: int = 3_600,
) -> dict[str, Any]:
    return validate_release_request(
        json.dumps(record),
        repository=repository,
        tag=tag,
        commit=commit,
        now_epoch=now_epoch,
        max_age_seconds=max_age_seconds,
    )


def test_valid_request_is_canonicalized() -> None:
    record = _record()

    canonical = canonical_release_request(
        json.dumps(record, indent=2),
        repository=REPOSITORY,
        tag=TAG,
        commit=COMMIT,
        now_epoch=NOW,
        max_age_seconds=3_600,
    )

    assert canonical == json.dumps(record, separators=(",", ":"), sort_keys=True)
    assert _validate(record) == record


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(extra=True), "field set"),
        (lambda value: value.update(schema_version="other"), "schema"),
        (lambda value: value.update(repository="other/repo"), "repository"),
        (lambda value: value.update(tag="v2.0.1"), "tag"),
        (lambda value: value.update(commit="b" * 40), "commit"),
        (
            lambda value: value.update(immutable_releases_enabled=False),
            "not observed as enabled",
        ),
        (lambda value: value.update(verified_at_epoch=1.0), "integer epoch"),
        (lambda value: value.update(verified_at_epoch=True), "integer epoch"),
    ],
)
def test_request_rejects_field_drift(
    mutate: Callable[[dict[str, Any]], object],
    message: str,
) -> None:
    record = _record()
    mutate(record)

    with pytest.raises(ReleaseRequestError, match=message):
        _validate(record)


def test_request_rejects_future_and_expired_records() -> None:
    future = _record()
    future["verified_at_epoch"] = NOW + 1
    with pytest.raises(ReleaseRequestError, match="future"):
        _validate(future)

    expired = _record()
    expired["verified_at_epoch"] = NOW - 3_601
    with pytest.raises(ReleaseRequestError, match="expired"):
        _validate(expired)


@pytest.mark.parametrize("tag", ["2.0.0", "v2.0", "v2.0.0rc1", "v02.0.0"])
def test_request_rejects_nonstable_expected_tag(tag: str) -> None:
    with pytest.raises(ReleaseRequestError, match="stable"):
        _validate(_record(), tag=tag)


def test_request_rejects_malformed_or_oversized_json() -> None:
    with pytest.raises(ReleaseRequestError, match="valid JSON"):
        validate_release_request(
            "{",
            repository=REPOSITORY,
            tag=TAG,
            commit=COMMIT,
            now_epoch=NOW,
            max_age_seconds=3_600,
        )
    with pytest.raises(ReleaseRequestError, match="byte limit"):
        validate_release_request(
            " " * 4_097,
            repository=REPOSITORY,
            tag=TAG,
            commit=COMMIT,
            now_epoch=NOW,
            max_age_seconds=3_600,
        )


def test_request_rejects_duplicate_json_fields() -> None:
    duplicate = json.dumps(_record())[:-1] + ',"tag":"v2.0.0"}'

    with pytest.raises(ReleaseRequestError, match="duplicate field: tag"):
        validate_release_request(
            duplicate,
            repository=REPOSITORY,
            tag=TAG,
            commit=COMMIT,
            now_epoch=NOW,
            max_age_seconds=3_600,
        )
