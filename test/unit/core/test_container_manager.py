"""
Unit tests for ContainerManager in jarvis_cd.core.container.
"""
import unittest
import os
import sys
import tempfile
import shutil
import yaml
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))


def _make_manager(tmp_dir):
    """Create a ContainerManager with containers_dir pointing to tmp_dir."""
    from jarvis_cd.core.container import ContainerManager

    mock_jarvis = MagicMock()
    with patch('jarvis_cd.core.container.Jarvis') as MockJarvis:
        MockJarvis.get_instance.return_value = mock_jarvis
        # Patch mkdir so __init__ doesn't fail on Path operations
        with patch.object(Path, 'mkdir'):
            cm = ContainerManager()

    # Override containers_dir to point to our temp directory
    cm.containers_dir = Path(tmp_dir)
    return cm


class TestContainerManagerList(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix='jarvis_test_container_')

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_list_containers_empty(self):
        """No yaml files in containers_dir → prints 'No containers found'."""
        cm = _make_manager(self.tmp_dir)
        with patch('builtins.print') as mock_print:
            cm.list_containers()
        printed = ' '.join(str(c) for call in mock_print.call_args_list for c in call[0])
        self.assertIn('No containers found', printed)

    def test_list_containers_with_manifests(self):
        """Two yaml manifests exist → output contains both container names."""
        for name in ('alpha', 'beta'):
            manifest = {'pkg_a': {}, 'pkg_b': {}}
            with open(os.path.join(self.tmp_dir, f'{name}.yaml'), 'w') as f:
                yaml.dump(manifest, f)

        cm = _make_manager(self.tmp_dir)
        printed_lines = []
        with patch('builtins.print', side_effect=lambda *a: printed_lines.append(' '.join(str(x) for x in a))):
            cm.list_containers()

        output = '\n'.join(printed_lines)
        self.assertIn('alpha', output)
        self.assertIn('beta', output)

    def test_list_containers_with_dockerfile(self):
        """yaml + Dockerfile present → status shows ✓."""
        name = 'mycontainer'
        with open(os.path.join(self.tmp_dir, f'{name}.yaml'), 'w') as f:
            yaml.dump({'pkg_a': {}}, f)
        open(os.path.join(self.tmp_dir, f'{name}.Dockerfile'), 'w').close()

        cm = _make_manager(self.tmp_dir)
        printed_lines = []
        with patch('builtins.print', side_effect=lambda *a: printed_lines.append(' '.join(str(x) for x in a))):
            cm.list_containers()

        output = '\n'.join(printed_lines)
        self.assertIn('✓', output)

    def test_list_containers_without_dockerfile(self):
        """yaml only (no Dockerfile) → status shows ✗."""
        name = 'nofile'
        with open(os.path.join(self.tmp_dir, f'{name}.yaml'), 'w') as f:
            yaml.dump({'pkg_a': {}}, f)

        cm = _make_manager(self.tmp_dir)
        printed_lines = []
        with patch('builtins.print', side_effect=lambda *a: printed_lines.append(' '.join(str(x) for x in a))):
            cm.list_containers()

        output = '\n'.join(printed_lines)
        self.assertIn('✗', output)


class TestContainerManagerRemove(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix='jarvis_test_container_remove_')

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _remove(self, name):
        """Call remove_container with Exec patched out."""
        cm = _make_manager(self.tmp_dir)
        mock_exec_instance = MagicMock()
        with patch('jarvis_cd.core.container.Jarvis'):
            with patch('jarvis_cd.shell.Exec') as MockExec, \
                 patch('jarvis_cd.shell.LocalExecInfo') as MockLEI:
                MockExec.return_value = mock_exec_instance
                cm.remove_container(name)

    def test_remove_container_removes_dockerfile(self):
        """remove_container() deletes the Dockerfile if it exists."""
        name = 'testimage'
        dockerfile = Path(self.tmp_dir) / f'{name}.Dockerfile'
        dockerfile.write_text('FROM ubuntu\n')

        self._remove(name)

        self.assertFalse(dockerfile.exists(), "Dockerfile should have been removed")

    def test_remove_container_removes_manifest(self):
        """remove_container() deletes the yaml manifest if it exists."""
        name = 'testimage'
        manifest = Path(self.tmp_dir) / f'{name}.yaml'
        manifest.write_text('pkg_a: {}\n')

        self._remove(name)

        self.assertFalse(manifest.exists(), "Manifest yaml should have been removed")

    def test_remove_container_missing_files(self):
        """remove_container() on a nonexistent name does not crash."""
        # Should not raise
        try:
            self._remove('does_not_exist')
        except Exception as e:
            self.fail(f"remove_container raised unexpectedly: {e}")


if __name__ == '__main__':
    unittest.main()
