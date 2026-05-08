"""
Unit tests for RouteApp / RouteService in jarvis_cd.core.route_pkg.
"""
import unittest
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))


def _make_route_app_instance():
    """
    Create a minimal concrete subclass of RouteApp and return an instance.
    Patches Jarvis.get_instance() so no real config is needed.
    """
    from jarvis_cd.core.route_pkg import RouteApp

    class ConcreteApp(RouteApp):
        """Minimal concrete subclass — inherits all RouteApp lifecycle methods."""
        pass

    mock_jarvis = MagicMock()
    mock_pipeline = MagicMock()

    with patch('jarvis_cd.core.config.Jarvis.get_instance', return_value=mock_jarvis):
        with patch.object(ConcreteApp, '_detect_pkg_dir', return_value=None):
            app = ConcreteApp.__new__(ConcreteApp)
            app.jarvis = mock_jarvis
            app.pipeline = mock_pipeline
            app.pkg_dir = None
            app.config_dir = None
            app.shared_dir = None
            app.private_dir = None
            app.env = {}
            app.mod_env = {}
            app.config = {'interceptors': {}}
            app.pkg_type = None
            app.global_id = None
            app.pkg_id = None

    return app


class TestRouteAppConfigureMenu(unittest.TestCase):

    def setUp(self):
        self.app = _make_route_app_instance()

    def test_configure_menu_has_deploy_mode(self):
        """_configure_menu() returns a list containing an item with name='deploy_mode'."""
        menu = self.app._configure_menu()
        names = [item['name'] for item in menu]
        self.assertIn('deploy_mode', names)

    def test_configure_menu_default_is_default(self):
        """The default value for deploy_mode is 'default'."""
        menu = self.app._configure_menu()
        deploy_item = next(item for item in menu if item['name'] == 'deploy_mode')
        self.assertEqual(deploy_item['default'], 'default')


class TestRouteAppDelegation(unittest.TestCase):

    def test_start_delegates_to_subclass(self):
        """start() calls _get_delegate(deploy_mode).start()."""
        app = _make_route_app_instance()
        app.config['deploy_mode'] = 'custom'

        mock_delegate = MagicMock()
        with patch.object(app, '_get_delegate', return_value=mock_delegate) as mock_get:
            app.start()

        mock_get.assert_called_once_with('custom')
        mock_delegate.start.assert_called_once()


class TestRouteService(unittest.TestCase):

    def test_route_service_is_importable(self):
        """RouteService is importable from jarvis_cd.core.route_pkg."""
        from jarvis_cd.core.route_pkg import RouteService
        self.assertTrue(issubclass(RouteService, object))

    def test_route_service_is_alias_for_route_app(self):
        """RouteService inherits from RouteApp."""
        from jarvis_cd.core.route_pkg import RouteApp, RouteService
        self.assertTrue(issubclass(RouteService, RouteApp))


if __name__ == '__main__':
    unittest.main()
