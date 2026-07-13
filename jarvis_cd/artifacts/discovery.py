"""Package-local artifact discovery for installed and filesystem repositories."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

from .provider import ArtifactProviderFactory, PackageArtifactProvider

_MODULE_LOAD_LOCK = threading.RLock()

if TYPE_CHECKING:
    from jarvis_cd.core.pkg import Pkg


def provider_from_package(package: "Pkg") -> PackageArtifactProvider | None:
    """Load an optional sibling ``artifacts.py`` for a package instance."""
    artifacts_path = _package_directory(package) / "artifacts.py"
    if not artifacts_path.is_file():
        return None
    artifacts_module = load_artifacts_module(artifacts_path)
    module_name = artifacts_module.__name__
    factory = getattr(artifacts_module, "adapter_from_package", None)
    if factory is None:
        raise TypeError(
            f"package artifact module must export adapter_from_package: {module_name}"
        )
    if not callable(factory):
        raise TypeError(
            f"package artifact adapter_from_package is not callable: {module_name}"
        )
    provider = cast(ArtifactProviderFactory, factory)(package_artifact_context(package))
    if provider is not None and not isinstance(provider, PackageArtifactProvider):
        raise TypeError(f"package artifact provider is incomplete: {module_name}")
    return provider


def load_artifacts_module(path: str | Path) -> ModuleType:
    """Load one package-local artifact module in an isolated namespace."""
    artifacts_path = Path(path).resolve()
    if not artifacts_path.is_file():
        raise FileNotFoundError(
            f"package artifact module does not exist: {artifacts_path}"
        )
    with _MODULE_LOAD_LOCK:
        return _load_artifacts_module_locked(artifacts_path)


def _load_artifacts_module_locked(artifacts_path: Path) -> ModuleType:
    """Load one resolved provider path while holding the module-cache lock."""
    digest = hashlib.sha256(str(artifacts_path).encode("utf-8")).hexdigest()[:24]
    package_name = f"_jarvis_cd_package_artifacts_{digest}"
    module_name = f"{package_name}.artifacts"
    cached = sys.modules.get(module_name)
    if cached is not None:
        cached_path = Path(str(getattr(cached, "__file__", ""))).resolve()
        if cached_path != artifacts_path:
            raise ImportError(
                f"package artifact module cache collision: {artifacts_path}"
            )
        return cached

    package_module = ModuleType(package_name)
    package_module.__file__ = str(artifacts_path.parent / "__init__.py")
    package_module.__package__ = package_name
    package_module.__path__ = [str(artifacts_path.parent)]
    spec = importlib.util.spec_from_file_location(module_name, artifacts_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load package artifact module: {artifacts_path}")
    artifacts_module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package_module
    sys.modules[module_name] = artifacts_module
    try:
        spec.loader.exec_module(artifacts_module)
    except BaseException:
        sys.modules.pop(module_name, None)
        sys.modules.pop(package_name, None)
        raise
    return artifacts_module


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


def package_artifact_context(package: "Pkg") -> dict[str, Any]:
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
                f"package artifacts require a non-empty {field_name} identity"
            )
        context[field_name] = value
    for field_name in ("shared_dir", "private_dir"):
        value = getattr(package, field_name, None)
        if isinstance(value, (str, os.PathLike)):
            context[field_name] = os.fspath(value)
    context["runtime_cwd"] = Path.cwd().resolve().as_posix()
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
