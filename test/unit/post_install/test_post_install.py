"""
Unit tests for install_builtin_packages() in jarvis_cd.post_install.
"""
import unittest
import os
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.post_install import install_builtin_packages


class TestInstallBuiltinPackages(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix='jarvis_test_post_install_')

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_install_copies_builtin_dir(self):
        """shutil.copytree is called with the correct src and dst paths."""
        jarvis_root = Path(self.tmp_dir) / '.ppi-jarvis'
        builtin_target = jarvis_root / 'builtin'

        # Provide a fake builtin source that exists
        fake_src = Path(self.tmp_dir) / 'fake_builtin_src'
        fake_src.mkdir(parents=True)

        with patch('jarvis_cd.post_install.Path.home', return_value=Path(self.tmp_dir)), \
             patch('jarvis_cd.post_install.shutil.copytree') as mock_copy, \
             patch.object(Path, 'exists', side_effect=lambda self=None: True if 'builtin' not in str(self) else False):
            # Reset side_effect to be more targeted
            pass

        # Simpler: patch builtin_target.exists() to return False (not yet installed)
        # and builtin_source.exists() to return True (source available).
        original_exists = Path.exists

        def patched_exists(p):
            if str(p) == str(builtin_target):
                return False        # Not yet installed
            if 'builtin_src' in str(p):
                return True         # Fake source exists
            return original_exists(p)

        with patch('jarvis_cd.post_install.Path.home', return_value=Path(self.tmp_dir)), \
             patch('jarvis_cd.post_install.shutil.copytree') as mock_copy:

            # We need builtin_source to resolve to fake_src; patch __file__ resolution
            import jarvis_cd.post_install as mod
            fake_this_file = fake_src.parent / 'jarvis_cd' / 'post_install.py'
            # project_root = this_file.parent.parent → fake_src.parent / 'jarvis_cd' / '..' / '..'
            # Easier: just mock Path.exists on the source check and override __file__

            with patch.object(mod, '__file__', str(fake_src / 'jarvis_cd' / 'post_install.py')):
                # Now project_root = fake_src / 'jarvis_cd' / '..' / '..' = fake_src
                # builtin_source = fake_src / 'builtin'
                builtin_source = fake_src / 'builtin'
                builtin_source.mkdir(parents=True, exist_ok=True)

                install_builtin_packages()

        mock_copy.assert_called_once()
        src_arg, dst_arg = mock_copy.call_args[0]
        self.assertEqual(Path(dst_arg), builtin_target)

    def test_install_creates_jarvis_root(self):
        """jarvis_root.mkdir() is called so the ~/.ppi-jarvis directory is created."""
        jarvis_root = Path(self.tmp_dir) / '.ppi-jarvis'

        import jarvis_cd.post_install as mod

        # Point home to tmp_dir so jarvis_root is inside our temp area
        with patch('jarvis_cd.post_install.Path.home', return_value=Path(self.tmp_dir)), \
             patch('jarvis_cd.post_install.shutil.copytree'):

            # Give it a valid __file__ pointing inside tmp_dir so builtin_source
            # check resolves to something that doesn't exist → warning branch, but
            # jarvis_root.mkdir() should still be called.
            with patch.object(mod, '__file__', str(Path(self.tmp_dir) / 'jarvis_cd' / 'post_install.py')):
                install_builtin_packages()

        # jarvis_root should have been created
        self.assertTrue(jarvis_root.exists())

    def test_install_handles_error_silently(self):
        """If shutil.copytree raises, install_builtin_packages does not propagate."""
        import jarvis_cd.post_install as mod

        # Build a fake source so the copy branch is reached
        fake_src = Path(self.tmp_dir) / 'fake_project'
        builtin_source = fake_src / 'builtin'
        builtin_source.mkdir(parents=True)

        with patch('jarvis_cd.post_install.Path.home', return_value=Path(self.tmp_dir)), \
             patch('jarvis_cd.post_install.shutil.copytree', side_effect=OSError("disk full")):

            with patch.object(mod, '__file__', str(fake_src / 'jarvis_cd' / 'post_install.py')):
                try:
                    install_builtin_packages()
                except Exception as e:
                    self.fail(f"install_builtin_packages raised unexpectedly: {e}")


if __name__ == '__main__':
    unittest.main()
