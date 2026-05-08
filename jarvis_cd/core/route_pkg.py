"""
Base classes for routed application deployment.
Provides routing functionality that delegates lifecycle methods to implementation-specific subclasses.
"""
from .pkg import Application, Service
from typing import Dict, Any, List


class RouteApp(Application):
    """
    Base class for routed application deployment.

    This class provides automatic delegation of lifecycle methods (start, stop, status, kill, clean)
    to implementation-specific subclasses based on the deploy_mode configuration parameter.

    Subclasses should:
    1. Override _configure_menu() to define package-specific parameters and available deploy modes
    2. Implement specific deployment classes (e.g., MyAppDefault, MyAppContainer)
    """

    def _configure_menu(self) -> List[Dict[str, Any]]:
        """
        Get the configuration menu including deploy_mode parameter.
        Subclasses should override this to specify available deployment modes.

        :return: List of configuration option dictionaries
        """
        return [
            {
                'name': 'deploy_mode',
                'msg': 'Deployment mode',
                'type': str,
                'default': 'default',
                'choices': ['default'],  # Subclasses should override with actual choices
            }
        ]

    def start(self):
        """
        Start the application using the appropriate implementation.
        """
        deploy_mode = self.config.get('deploy_mode', 'default')
        self._get_delegate(deploy_mode).start()

    def stop(self):
        """
        Stop the application using the appropriate implementation.
        """
        deploy_mode = self.config.get('deploy_mode', 'default')
        self._get_delegate(deploy_mode).stop()

    def status(self):
        """
        Get status of the application using the appropriate implementation.
        """
        deploy_mode = self.config.get('deploy_mode', 'default')
        return self._get_delegate(deploy_mode).status()

    def kill(self):
        """
        Kill the application using the appropriate implementation.
        """
        deploy_mode = self.config.get('deploy_mode', 'default')
        self._get_delegate(deploy_mode).kill()

    def clean(self):
        """
        Clean application data using the appropriate implementation.
        """
        deploy_mode = self.config.get('deploy_mode', 'default')
        self._get_delegate(deploy_mode).clean()


class RouteService(RouteApp):
    """
    Alias for RouteApp following service naming conventions.
    """
    pass
