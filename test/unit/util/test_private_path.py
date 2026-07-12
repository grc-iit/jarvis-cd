"""Owner-private state path tests."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jarvis_cd.util.private_path import (
    ensure_private_descriptor,
    ensure_private_path,
    reject_private_path_redirection,
)


def test_private_paths_are_normalized_under_a_default_parent(tmp_path: Path) -> None:
    """State paths do not retain broad permissions from their parent."""
    directory = tmp_path / "state"
    directory.mkdir()
    path = directory / "record.json"
    path.write_text("{}\n", encoding="utf-8")

    ensure_private_path(directory, directory=True)
    ensure_private_path(path, directory=False)

    if os.name != "nt":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        return

    import ntsecuritycon
    import win32security

    for target, expected_flags in (
        (
            directory,
            win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE,
        ),
        (path, 0),
    ):
        descriptor = win32security.GetNamedSecurityInfo(
            str(target),
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION
            | win32security.DACL_SECURITY_INFORMATION,
        )
        control, _revision = descriptor.GetSecurityDescriptorControl()
        assert control & 0x1000
        dacl = descriptor.GetSecurityDescriptorDacl()
        assert dacl is not None
        assert dacl.GetAceCount() == 3
        for index in range(dacl.GetAceCount()):
            header, mask, _sid = dacl.GetAce(index)
            assert header == (win32security.ACCESS_ALLOWED_ACE_TYPE, expected_flags)
            assert mask == ntsecuritycon.FILE_ALL_ACCESS


def test_private_path_rejects_redirected_ancestor_before_creation(
    tmp_path: Path,
) -> None:
    """A symlink or NTFS junction cannot relocate private state."""
    target = tmp_path / "outside"
    target.mkdir()
    redirected = tmp_path / "redirected"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(redirected))
    else:
        redirected.symlink_to(target, target_is_directory=True)

    candidate = redirected / "not-created"
    with pytest.raises(RuntimeError, match="symbolic link or reparse point"):
        reject_private_path_redirection(candidate)
    assert not (target / "not-created").exists()


def test_private_descriptor_normalizes_the_held_object(tmp_path: Path) -> None:
    """Descriptor-bound normalization protects the object used for I/O."""
    path = tmp_path / "record.json"
    path.write_text("{}\n", encoding="utf-8")
    descriptor = os.open(path, os.O_RDWR)
    try:
        ensure_private_descriptor(path, descriptor, directory=False)
        information = os.fstat(descriptor)
        assert information.st_nlink == 1
        assert (information.st_dev, information.st_ino) == (
            path.lstat().st_dev,
            path.lstat().st_ino,
        )
        if os.name != "nt":
            assert stat.S_IMODE(information.st_mode) == 0o600
    finally:
        os.close(descriptor)


def test_private_file_rejects_hard_link_alias(tmp_path: Path) -> None:
    """Private state cannot retain a second pathname outside its directory."""
    path = tmp_path / "record.json"
    path.write_text("{}\n", encoding="utf-8")
    os.link(path, tmp_path / "alias.json")

    with pytest.raises(RuntimeError, match="exactly one link"):
        ensure_private_path(path, directory=False)
