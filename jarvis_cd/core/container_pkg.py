"""
Deprecated: Container support is now built into Application (jarvis_cd/core/pkg.py).
This module is kept for backwards compatibility only.
"""
from .pkg import Application, Service

# Aliases kept for any external code that might import these
ContainerApplication = Application
ContainerService = Service
