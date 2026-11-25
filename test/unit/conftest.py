"""Shared unit-test setup."""
import pytest


@pytest.fixture(autouse=True)
def _no_site_copy_search(monkeypatch, tmp_path):
    """Keep Spack package tests off the host's module system and site Spacks.

    The site-copy search (``jarvis_cd.core.site_copies``) runs real ``module``
    and ``spack find`` calls, and the Spack health probe
    (``jarvis_cd.core.spack_health``) runs ``spack providers mpi``; their own
    tests turn them back on with fakes. Its scope copies go under the test's
    temporary directory, not the site home. An install's temp directory
    needs no free-space margin, so a full CI disk fails only tests that
    write.
    """
    from jarvis_cd.core import spack_health

    monkeypatch.setenv("JARVIS_SITE_COPIES", "off")
    monkeypatch.setenv("JARVIS_SPACK_HEALTH", "off")
    monkeypatch.setenv("JARVIS_SPACK_SCOPE_DIR", str(tmp_path / "spack-scopes"))
    monkeypatch.setenv("JARVIS_INSTALL_TMP_MIN_MB", "0")
    monkeypatch.delenv("JARVIS_INSTALL_TMPDIR", raising=False)
    spack_health.reset()
    yield
    spack_health.reset()
