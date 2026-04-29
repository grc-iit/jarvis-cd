"""
Unit tests for IOR pipeline with a simulated Docker cluster.
Tests that an IOR pipeline correctly configures a hostfile for
a set of simulated Docker container hosts with 4 processes (1 per node).
"""
import unittest
import sys
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.pipeline import Pipeline
from jarvis_cd.util.hostfile import Hostfile


def initialize_jarvis_for_test(config_dir, private_dir, shared_dir):
    """Helper function to properly initialize Jarvis for testing"""
    jarvis = Jarvis.get_instance()
    saved_config = None
    if jarvis.config_file.exists():
        import yaml
        with open(jarvis.config_file, 'r') as f:
            saved_config = yaml.safe_load(f)
    jarvis.initialize(config_dir, private_dir, shared_dir, force=False)
    return jarvis, saved_config


class TestIorDockerCluster(unittest.TestCase):
    """Test IOR pipeline with a simulated 4-node Docker cluster."""

    # Simulated Docker container hostnames
    DOCKER_HOSTS = [
        'ior-node-01',
        'ior-node-02',
        'ior-node-03',
        'ior-node-04',
    ]

    def setUp(self):
        """Set up test environment with Jarvis directories and a hostfile."""
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_docker_cluster_')
        self.config_dir = os.path.join(self.test_dir, 'config')
        self.private_dir = os.path.join(self.test_dir, 'private')
        self.shared_dir = os.path.join(self.test_dir, 'shared')
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.private_dir, exist_ok=True)
        os.makedirs(self.shared_dir, exist_ok=True)

        os.environ['JARVIS_CONFIG'] = self.config_dir
        os.environ['JARVIS_PRIVATE'] = self.private_dir
        os.environ['JARVIS_SHARED'] = self.shared_dir

        self.jarvis, self._saved_config = initialize_jarvis_for_test(
            self.config_dir, self.private_dir, self.shared_dir
        )

        # Write a hostfile with the simulated Docker hosts
        self.hostfile_path = os.path.join(self.test_dir, 'docker_hostfile')
        with open(self.hostfile_path, 'w') as f:
            f.write('\n'.join(self.DOCKER_HOSTS) + '\n')

    def tearDown(self):
        """Clean up test environment."""
        if self._saved_config:
            import yaml
            jarvis = Jarvis.get_instance()
            jarvis.save_config(self._saved_config)
            jarvis.config_dir = self._saved_config.get('config_dir', jarvis.config_dir)
            jarvis.private_dir = self._saved_config.get('private_dir', jarvis.private_dir)
            jarvis.shared_dir = self._saved_config.get('shared_dir', jarvis.shared_dir)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    # ------------------------------------------------------------------
    # Helper to build an IOR package definition
    # ------------------------------------------------------------------

    def _make_ior_pkg_def(self, pipeline_name, nprocs=4, ppn=1):
        """Return a package definition dict for the IOR package."""
        return {
            'pkg_type': 'builtin.ior',
            'pkg_id': 'ior_cluster',
            'pkg_name': 'ior',
            'global_id': f'{pipeline_name}.ior_cluster',
            'config': {
                'nprocs': nprocs,
                'ppn': ppn,
                'block': '64m',
                'xfer': '1m',
                'api': 'posix',
                'out': '/tmp/ior_cluster.bin',
                'log': '/tmp/ior_cluster.log',
                'write': True,
                'read': False,
                'fpp': False,
                'reps': 1,
                'direct': False,
                'interceptors': [],
            },
        }

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_hostfile_has_four_docker_hosts(self):
        """The hostfile should parse exactly 4 Docker container hostnames."""
        hf = Hostfile(path=self.hostfile_path, find_ips=False)

        self.assertEqual(len(hf), 4)
        self.assertEqual(hf.hosts, self.DOCKER_HOSTS)

    def test_pipeline_uses_docker_hostfile(self):
        """Pipeline should store and persist the Docker hostfile."""
        pipeline = Pipeline()
        pipeline.create('docker_cluster_test')

        hf = Hostfile(path=self.hostfile_path, find_ips=False)
        pipeline.hostfile = hf
        pipeline.save()

        # Reload and verify
        pipeline2 = Pipeline('docker_cluster_test')
        effective = pipeline2.get_hostfile()
        self.assertIsNotNone(effective)
        self.assertEqual(len(effective.hosts), 4)

    def test_ior_package_inherits_pipeline_hostfile(self):
        """IOR package should fall back to the pipeline hostfile."""
        pipeline = Pipeline()
        pipeline.create('docker_cluster_pkg')

        hf = Hostfile(path=self.hostfile_path, find_ips=False)
        pipeline.hostfile = hf
        pipeline.save()

        pkg_def = self._make_ior_pkg_def(pipeline.name)
        pipeline.packages.append(pkg_def)
        pipeline.save()

        pkg_instance = pipeline._load_package_instance(pkg_def, {})
        pkg_hostfile = pkg_instance.get_hostfile()

        self.assertIsNotNone(pkg_hostfile)
        self.assertEqual(len(pkg_hostfile.hosts), 4)
        self.assertEqual(pkg_hostfile.hosts, self.DOCKER_HOSTS)

    def test_ior_nprocs_matches_node_count(self):
        """nprocs=4 and ppn=1 should mean 1 process per Docker node."""
        pipeline = Pipeline()
        pipeline.create('docker_cluster_nprocs')

        hf = Hostfile(path=self.hostfile_path, find_ips=False)
        pipeline.hostfile = hf
        pipeline.save()

        pkg_def = self._make_ior_pkg_def(pipeline.name, nprocs=4, ppn=1)
        pipeline.packages.append(pkg_def)
        pipeline.save()

        pkg_instance = pipeline._load_package_instance(pkg_def, {})

        self.assertEqual(pkg_instance.config['nprocs'], 4)
        self.assertEqual(pkg_instance.config['ppn'], 1)
        # 4 procs / 1 ppn == 4 nodes required, matching the hostfile
        self.assertEqual(
            pkg_instance.config['nprocs'] // pkg_instance.config['ppn'],
            len(self.DOCKER_HOSTS),
        )

    def test_ior_container_install_manager(self):
        """Pipeline should use install_manager=container for containerized deployment."""
        pipeline = Pipeline()
        pipeline.create('docker_cluster_mode')
        pipeline.install_manager = 'container'
        pipeline.container_engine = 'docker'
        pipeline.container_base = 'ubuntu:24.04'

        hf = Hostfile(path=self.hostfile_path, find_ips=False)
        pipeline.hostfile = hf

        pkg_def = self._make_ior_pkg_def(pipeline.name)
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()

        pkg_instance = pipeline._load_package_instance(pkg_def, {})
        self.assertEqual(pkg_instance.config['deploy_mode'], 'container')

        # Verify container-related pipeline settings persisted
        pipeline2 = Pipeline('docker_cluster_mode')
        self.assertEqual(pipeline2.install_manager, 'container')
        self.assertEqual(pipeline2.container_engine, 'docker')
        self.assertEqual(pipeline2.container_base, 'ubuntu:24.04')

    def test_pipeline_container_engine_docker(self):
        """Pipeline container_engine should be 'docker'."""
        pipeline = Pipeline()
        pipeline.create('docker_cluster_engine')
        pipeline.container_engine = 'docker'

        hf = Hostfile(path=self.hostfile_path, find_ips=False)
        pipeline.hostfile = hf
        pipeline.save()

        # Reload
        pipeline2 = Pipeline('docker_cluster_engine')
        self.assertEqual(pipeline2.container_engine, 'docker')

    def test_compose_mounts_shared_and_private_at_same_path(self):
        """
        The generated docker-compose file should mount shared_dir and
        private_dir at identical host:container paths so that all
        configuration paths work the same on host and inside the container.
        """
        pipeline = Pipeline()
        pipeline.create('docker_cluster_compose')
        pipeline.container_engine = 'docker'
        pipeline.container_image = 'ior-test:latest'

        hf = Hostfile(path=self.hostfile_path, find_ips=False)
        pipeline.hostfile = hf
        pipeline.save()

        compose_path = pipeline._generate_pipeline_compose_file()
        self.assertTrue(os.path.exists(compose_path))

        import yaml
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)

        service = compose['services']['docker_cluster_compose']
        volumes = service['volumes']

        shared_dir = str(self.jarvis.get_pipeline_shared_dir('docker_cluster_compose'))
        private_dir = str(self.jarvis.get_pipeline_private_dir('docker_cluster_compose'))

        # Shared and private dirs must be mounted at the same path
        self.assertIn(f"{shared_dir}:{shared_dir}", volumes)
        self.assertIn(f"{private_dir}:{private_dir}", volumes)

    def test_hostfile_saved_into_shared_dir(self):
        """
        When a hostfile is set, save() should copy it into the pipeline's
        shared directory so it is on the volume shared between host and
        container.  The saved pipeline.yaml should reference that shared
        path — the same path is valid on both host and container.
        """
        pipeline = Pipeline()
        pipeline.create('docker_cluster_path')
        pipeline.container_image = 'ior-test:latest'

        hf = Hostfile(path=self.hostfile_path, find_ips=False)
        pipeline.hostfile = hf
        pipeline.save()

        shared_dir = self.jarvis.get_pipeline_shared_dir('docker_cluster_path')
        expected_hostfile = str(shared_dir / 'hostfile')

        # The hostfile should physically exist in shared_dir
        self.assertTrue(os.path.exists(expected_hostfile))

        # Its content should match the original hosts
        saved_hf = Hostfile(path=expected_hostfile, find_ips=False)
        self.assertEqual(saved_hf.hosts, self.DOCKER_HOSTS)

        # pipeline.yaml should reference the shared_dir path (not /root/...)
        import yaml
        config_file = (
            self.jarvis.get_pipeline_dir('docker_cluster_path') / 'pipeline.yaml'
        )
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f)

        self.assertEqual(config['hostfile'], expected_hostfile)

    def test_compose_container_runs_ssh_and_sleeps(self):
        """
        The container entrypoint should start SSH and sleep forever.
        The host-side jarvis orchestrates — no jarvis needed inside.
        """
        pipeline = Pipeline()
        pipeline.create('docker_cluster_cmd')
        pipeline.container_engine = 'docker'
        pipeline.container_image = 'ior-test:latest'
        pipeline.save()

        compose_path = pipeline._generate_pipeline_compose_file()

        import yaml
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)

        service = compose['services']['docker_cluster_cmd']
        container_cmd = service['command'][0]

        self.assertIn('/usr/sbin/sshd', container_cmd)
        self.assertIn('sleep infinity', container_cmd)
        self.assertNotIn('jarvis', container_cmd)


if __name__ == '__main__':
    unittest.main()
