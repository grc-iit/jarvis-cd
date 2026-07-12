"""Package-local progress discovery for installed and filesystem repositories."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import threading
from types import ModuleType
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .provider import PackageProgressProvider, ProgressProviderFactory

_MODULE_LOAD_LOCK = threading.RLock()

if TYPE_CHECKING:
    from jarvis_cd.core.pkg import Pkg


def provider_from_package(package: "Pkg") -> PackageProgressProvider | None:
    """Load the optional sibling ``progress.py`` for a package instance.

    JARVIS repository packages are imported from their registered filesystem
    roots. Resolving relative to the already-loaded ``pkg.py`` module therefore
    works without requiring the repository to publish a Python distribution or
    an entry point.
    """
    progress_path = _package_directory(package) / "progress.py"
    if not progress_path.is_file():
        return None
    progress_module = load_progress_module(progress_path)
    progress_module_name = progress_module.__name__
    factory = getattr(progress_module, "adapter_from_package", None)
    if factory is None:
        raise TypeError(
            f"package progress module must export adapter_from_package: "
            f"{progress_module_name}"
        )
    if not callable(factory):
        raise TypeError(
            f"package progress adapter_from_package is not callable: "
            f"{progress_module_name}"
        )
    context = package_progress_context(package)
    provider = cast(ProgressProviderFactory, factory)(context)
    if provider is not None and not isinstance(provider, PackageProgressProvider):
        raise TypeError(
            f"package progress provider is incomplete: {progress_module_name}"
        )
    return provider


def load_progress_module(path: str | Path) -> ModuleType:
    """Load one package-local progress module in an isolated namespace.

    JARVIS repositories deliberately reuse short top-level names such as
    ``builtin``. Importing progress providers through those names can collide
    with the distribution's own package cache. A path-derived private package
    preserves sibling relative imports without mutating repository namespaces.

    :param path: Absolute or relative path to a package's ``progress.py``.
    :return: The loaded progress module.
    """
    progress_path = Path(path).resolve()
    if not progress_path.is_file():
        raise FileNotFoundError(
            f"package progress module does not exist: {progress_path}"
        )
    with _MODULE_LOAD_LOCK:
        return _load_progress_module_locked(progress_path)


def _load_progress_module_locked(progress_path: Path) -> ModuleType:
    """Load one resolved provider path while holding the module-cache lock."""
    digest = hashlib.sha256(str(progress_path).encode("utf-8")).hexdigest()[:24]
    package_name = f"_jarvis_cd_package_progress_{digest}"
    module_name = f"{package_name}.progress"
    cached = sys.modules.get(module_name)
    if cached is not None:
        cached_path = Path(str(getattr(cached, "__file__", ""))).resolve()
        if cached_path != progress_path:
            raise ImportError(
                f"package progress module cache collision: {progress_path}"
            )
        return cached

    package_module = ModuleType(package_name)
    package_module.__file__ = str(progress_path.parent / "__init__.py")
    package_module.__package__ = package_name
    package_module.__path__ = [str(progress_path.parent)]
    spec = importlib.util.spec_from_file_location(module_name, progress_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load package progress module: {progress_path}")
    progress_module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package_module
    sys.modules[module_name] = progress_module
    try:
        spec.loader.exec_module(progress_module)
    except BaseException:
        sys.modules.pop(module_name, None)
        sys.modules.pop(package_name, None)
        raise
    return progress_module


def _package_directory(package: "Pkg") -> Path:
    """Resolve the source directory for a loaded JARVIS package instance."""
    configured = getattr(package, "pkg_dir", None)
    if configured:
        return Path(str(configured)).resolve()
    module_name = package.__class__.__module__
    module = sys.modules.get(module_name)
    module_path = getattr(module, "__file__", None)
    if not module_path:
        raise ValueError(
            f"cannot locate package source directory for {package.pkg_type}"
        )
    return Path(str(module_path)).resolve().parent


def package_progress_context(package: "Pkg") -> dict[str, Any]:
    """Build the generic, non-secret context supplied to a provider factory."""
    context: dict[str, Any] = dict(package.config)
    identities: dict[str, object] = {
        "pkg_type": package.pkg_type,
        "pkg_id": package.pkg_id,
        "global_id": package.global_id,
    }
    for field_name, value in identities.items():
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"package progress requires a non-empty {field_name} identity"
            )
        context[field_name] = value
    pipeline = package.pipeline
    for field_name in ("base_deploy_mode", "install_manager"):
        value = getattr(pipeline, field_name, None)
        if value is not None:
            context[field_name] = value
    package_mode = context.get("deploy_mode") or context.get("install_method")
    base_mode = context.get("base_deploy_mode") or context.get("install_manager")
    effective_mode = package_mode or base_mode
    if effective_mode:
        context["effective_deploy_mode"] = effective_mode
    return context
