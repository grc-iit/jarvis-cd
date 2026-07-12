"""Cross-platform owner-private state-path enforcement."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any


def reject_private_path_redirection(path: Path) -> None:
    """Reject symbolic links and Windows reparse points in a path ancestry.

    The final path is allowed not to exist so callers can perform this check
    before creating private state.  The check intentionally uses ``lstat`` and
    an absolute, unresolved path: resolving first would hide the redirection
    that this boundary is meant to reject.

    :param path: Prospective or existing private-state path.
    """
    current = Path(os.path.abspath(path))
    existing: list[tuple[Path, os.stat_result]] = []
    while True:
        try:
            information = current.lstat()
        except FileNotFoundError:
            pass
        else:
            existing.append((current, information))
        if current == current.parent:
            break
        current = current.parent
    for component, information in reversed(existing):
        if _is_path_redirection(information):
            raise RuntimeError(
                "private path cannot traverse a symbolic link or reparse point: "
                f"{component}"
            )


def ensure_private_path(path: Path, *, directory: bool) -> None:
    """Normalize and verify one owner-controlled file or directory.

    POSIX paths use owner/mode checks. Windows paths receive a protected DACL
    granting full control only to the current user, LocalSystem, and local
    Administrators; reparse points are rejected on both platforms.

    :param path: Existing path to protect and verify.
    :param directory: Whether the path must be a directory.
    """
    target = Path(os.path.abspath(path))
    reject_private_path_redirection(target)
    information = target.lstat()
    expected_type = (
        stat.S_ISDIR(information.st_mode)
        if directory
        else stat.S_ISREG(information.st_mode)
    )
    if not expected_type or stat.S_ISLNK(information.st_mode):
        kind = "directory" if directory else "file"
        raise RuntimeError(f"private path is not a regular {kind}: {target}")
    if not directory and information.st_nlink != 1:
        raise RuntimeError(f"private file must have exactly one link: {target}")
    if os.name == "nt":
        _ensure_private_windows_path(target, directory=directory)
        return
    getuid = getattr(os, "geteuid", None)
    if not callable(getuid) or information.st_uid != getuid():
        raise RuntimeError(f"private path is not owned by this user: {target}")
    target.chmod(0o700 if directory else 0o600)
    verified = target.lstat()
    if stat.S_IMODE(verified.st_mode) & 0o077:
        raise RuntimeError(f"private path permissions are too broad: {target}")


def ensure_private_descriptor(
    path: Path,
    descriptor: int,
    *,
    directory: bool,
) -> None:
    """Normalize and verify the exact object held by a file descriptor.

    This closes the path-normalization/open race for private records and lock
    files.  On Windows, ``ReOpenFile`` obtains the security rights on the same
    kernel object rather than reopening its potentially replaced pathname.

    :param path: Path that must still identify ``descriptor``.
    :param descriptor: Open operating-system file descriptor.
    :param directory: Whether the held object must be a directory.
    """
    target = Path(os.path.abspath(path))
    reject_private_path_redirection(target)
    descriptor_information = os.fstat(descriptor)
    path_information = target.lstat()
    expected_type = (
        stat.S_ISDIR(descriptor_information.st_mode)
        if directory
        else stat.S_ISREG(descriptor_information.st_mode)
    )
    if (
        not expected_type
        or _is_path_redirection(descriptor_information)
        or _is_path_redirection(path_information)
        or (descriptor_information.st_dev, descriptor_information.st_ino)
        != (path_information.st_dev, path_information.st_ino)
    ):
        raise RuntimeError(f"private path changed during secure open: {target}")
    if not directory and descriptor_information.st_nlink != 1:
        raise RuntimeError(f"private file must have exactly one link: {target}")
    if os.name == "nt":
        _ensure_private_windows_descriptor(
            target,
            descriptor,
            directory=directory,
        )
    else:
        getuid = getattr(os, "geteuid", None)
        if not callable(getuid) or descriptor_information.st_uid != getuid():
            raise RuntimeError(f"private path is not owned by this user: {target}")
        os.fchmod(descriptor, 0o700 if directory else 0o600)
        verified = os.fstat(descriptor)
        if stat.S_IMODE(verified.st_mode) & 0o077:
            raise RuntimeError(f"private path permissions are too broad: {target}")
    final_path_information = target.lstat()
    final_descriptor_information = os.fstat(descriptor)
    if _is_path_redirection(final_path_information) or (
        final_descriptor_information.st_dev,
        final_descriptor_information.st_ino,
    ) != (final_path_information.st_dev, final_path_information.st_ino):
        raise RuntimeError(f"private path changed during secure open: {target}")


def _ensure_private_windows_path(path: Path, *, directory: bool) -> None:
    """Open a no-follow handle and apply a protected Windows DACL."""
    import win32api
    import win32con
    import win32file

    handle = win32file.CreateFile(
        str(path),
        win32con.READ_CONTROL | win32con.WRITE_DAC,
        win32file.FILE_SHARE_READ
        | win32file.FILE_SHARE_WRITE
        | win32file.FILE_SHARE_DELETE,
        None,
        win32con.OPEN_EXISTING,
        win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32file.FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    try:
        _ensure_private_windows_handle(path, handle, directory=directory)
        _verify_windows_path_identity(path, handle)
    finally:
        close_handle: Any = win32api.CloseHandle
        close_handle(handle)


def _ensure_private_windows_descriptor(
    path: Path,
    descriptor: int,
    *,
    directory: bool,
) -> None:
    """Apply a protected DACL to the exact object held by ``descriptor``."""
    import msvcrt
    import win32api
    import win32con
    import win32file

    operating_system_handle = msvcrt.get_osfhandle(descriptor)
    handle = win32file.ReOpenFile(
        operating_system_handle,
        win32con.READ_CONTROL | win32con.WRITE_DAC,
        win32file.FILE_SHARE_READ
        | win32file.FILE_SHARE_WRITE
        | win32file.FILE_SHARE_DELETE,
        win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32file.FILE_FLAG_BACKUP_SEMANTICS,
    )
    try:
        _ensure_private_windows_handle(path, handle, directory=directory)
    finally:
        close_handle: Any = win32api.CloseHandle
        close_handle(handle)


def _ensure_private_windows_handle(
    path: Path,
    handle: Any,
    *,
    directory: bool,
) -> None:
    """Apply and read back the private ACL on one pinned Windows handle."""
    import ntsecuritycon
    import win32api
    import win32file
    import win32security

    information = win32file.GetFileInformationByHandle(handle)
    attributes = int(information[0])
    is_directory = bool(attributes & stat.FILE_ATTRIBUTE_DIRECTORY)
    if is_directory != directory or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        kind = "directory" if directory else "file"
        raise RuntimeError(f"private Windows path is not a regular {kind}: {path}")
    if not directory and int(information[7]) != 1:
        raise RuntimeError(f"private file must have exactly one link: {path}")

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32security.TOKEN_QUERY,
    )
    try:
        user_sid = win32security.GetTokenInformation(
            token,
            win32security.TokenUser,
        )[0]
    finally:
        close_handle: Any = win32api.CloseHandle
        close_handle(token)
    system_sid = win32security.CreateWellKnownSid(
        win32security.WinLocalSystemSid,
        None,
    )
    administrators_sid = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid,
        None,
    )
    initial_descriptor = win32security.GetSecurityInfo(
        handle,
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION,
    )
    owner = initial_descriptor.GetSecurityDescriptorOwner()
    if owner is None or _sid_text(owner, win32security) != _sid_text(
        user_sid,
        win32security,
    ):
        raise RuntimeError(f"private Windows path has the wrong owner: {path}")
    inherit_flags = (
        win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
        if directory
        else 0
    )
    dacl = win32security.ACL()
    expected_sids = (user_sid, system_sid, administrators_sid)
    for sid in expected_sids:
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            inherit_flags,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
    set_security_info: Any = win32security.SetSecurityInfo
    set_security_info(
        handle,
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )
    descriptor = win32security.GetSecurityInfo(
        handle,
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION,
    )
    owner = descriptor.GetSecurityDescriptorOwner()
    if _sid_text(owner, win32security) != _sid_text(user_sid, win32security):
        raise RuntimeError(f"private Windows path has the wrong owner: {path}")
    control, _revision = descriptor.GetSecurityDescriptorControl()
    if not control & 0x1000:  # SE_DACL_PROTECTED
        raise RuntimeError(f"private Windows path still inherits permissions: {path}")
    verified_dacl = descriptor.GetSecurityDescriptorDacl()
    if verified_dacl is None or verified_dacl.GetAceCount() != len(expected_sids):
        raise RuntimeError(f"private Windows path has an unexpected ACL: {path}")
    expected_sid_text = {_sid_text(sid, win32security) for sid in expected_sids}
    actual_sid_text: set[str] = set()
    for index in range(verified_dacl.GetAceCount()):
        header, access_mask, sid = verified_dacl.GetAce(index)
        ace_type, ace_flags = header
        if (
            ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE
            or ace_flags != inherit_flags
            or access_mask != ntsecuritycon.FILE_ALL_ACCESS
        ):
            raise RuntimeError(f"private Windows path grants unexpected access: {path}")
        actual_sid_text.add(_sid_text(sid, win32security))
    if actual_sid_text != expected_sid_text:
        raise RuntimeError(f"private Windows path ACL is not owner-private: {path}")


def _verify_windows_path_identity(path: Path, handle: Any) -> None:
    """Confirm that a pinned Windows handle still has the requested pathname."""
    import win32file

    information = win32file.GetFileInformationByHandle(handle)
    handle_file_id = (int(information[8]) << 32) | int(information[9])
    path_information = path.lstat()
    if (
        _is_path_redirection(path_information)
        or path_information.st_ino != handle_file_id
    ):
        raise RuntimeError(f"private path changed during secure open: {path}")


def _is_path_redirection(information: os.stat_result) -> bool:
    """Return whether an ``lstat``/``fstat`` result denotes redirection."""
    attributes = getattr(information, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(information.st_mode) or bool(attributes & reparse_flag)


def _sid_text(sid: Any, win32security: Any) -> str:
    """Return a stable string form for a pywin32 SID object."""
    return str(win32security.ConvertSidToStringSid(sid))
