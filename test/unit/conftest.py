"""Shared unit-test setup."""
import pytest


@pytest.fixture(autouse=True)
def _no_site_copy_search(monkeypatch):
    """Keep Spack package tests off the host's module system and site Spacks.

    The site-copy search (``jarvis_cd.core.site_copies``) runs real ``module``
    and ``spack find`` calls; its own tests turn it back on with fakes.
    """
    monkeypatch.setenv("JARVIS_SITE_COPIES", "off")
