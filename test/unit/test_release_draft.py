"""Tests for fail-closed GitHub draft release discovery."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest

from ci.release_draft import (
    ReleaseDraftError,
    ReleaseDraftNotFound,
    main,
    select_exact_draft,
)

TAG = "v1.2.0"
TITLE = TAG
BODY = f"JARVIS-CD {TAG} immutable release."
AUTHOR = "github-actions[bot]"


def _draft(**overrides: Any) -> dict[str, Any]:
    release: dict[str, Any] = {
        "id": 123,
        "tag_name": TAG,
        "name": TITLE,
        "body": BODY,
        "draft": True,
        "prerelease": False,
        "immutable": False,
        "author": {"login": AUTHOR},
        "assets": [],
    }
    release.update(overrides)
    return release


def _select(pages: object) -> dict[str, Any]:
    return select_exact_draft(
        json.dumps(pages),
        tag=TAG,
        title=TITLE,
        body=BODY,
        author=AUTHOR,
    )


def test_exact_draft_is_selected_across_paginated_results() -> None:
    draft = _draft()

    assert (
        _select([[{"id": 1, "tag_name": "v1.1.0", "draft": False}], [draft]]) == draft
    )


def test_missing_and_duplicate_tag_drafts_are_rejected() -> None:
    with pytest.raises(ReleaseDraftNotFound, match="no draft"):
        _select([[]])
    with pytest.raises(ReleaseDraftError, match="draft"):
        _select([[{"id": 1, "tag_name": TAG, "draft": False}]])

    with pytest.raises(ReleaseDraftError, match="multiple releases"):
        _select([[_draft(id=1)], [_draft(id=2)]])


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(name="wrong"), "name"),
        (lambda value: value.update(body="wrong"), "body"),
        (lambda value: value.update(prerelease=True), "prerelease"),
        (lambda value: value.update(immutable=True), "immutable"),
        (lambda value: value.update(author={"login": "someone"}), "author"),
        (lambda value: value.update(id=0), "positive integer"),
        (lambda value: value.update(assets={}), "assets"),
    ],
)
def test_draft_metadata_drift_is_rejected(
    mutate: Callable[[dict[str, Any]], object],
    message: str,
) -> None:
    draft = _draft()
    mutate(draft)

    with pytest.raises(ReleaseDraftError, match=message):
        _select([[draft]])


@pytest.mark.parametrize("document", [[], {}, [[None]]])
def test_malformed_paginated_responses_are_rejected(document: object) -> None:
    expected = "at least one API page" if document == [] else "release list|entries"
    with pytest.raises(ReleaseDraftError, match=expected):
        _select(document)


def test_cli_uses_status_three_only_for_an_absent_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", StringIO("[[]]"))

    assert (
        main(
            [
                "--tag",
                TAG,
                "--title",
                TITLE,
                "--body",
                BODY,
                "--author",
                AUTHOR,
            ]
        )
        == 3
    )
