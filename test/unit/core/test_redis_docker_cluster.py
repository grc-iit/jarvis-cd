"""
Unit tests for a Redis + Redis-benchmark pipeline on a simulated Docker cluster.
Verifies that a pipeline with a Redis service and redis-benchmark application
correctly configures a hostfile for 4 simulated Docker hosts and that
shared/private directories are mounted at identical paths on host and container.
"""
import unittest
import sys
import os
import tempfile
import shutil
import yaml
from pathlib import Path

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


class TestRedisDockerCluster(unittest.TestCase):
    """Test Redis + redis-benchmark pipeline on a simulated 4-node Docker cluster."""

    DOCKER_HOSTS = [
        'redis-node-01',
        'redis-node-02',
        'redis-node-03',
        'redis-node-04',
    ]

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_redis_cluster_')
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
            self.config_dir, self.private_dir, self.shared_dir,
        )

        # Write a hostfile with the simulated Docker hosts
        self.hostfile_path = os.path.join(self.test_dir, 'docker_hostfile')
        with open(self.hostfile_path, 'w') as f:
            f.write('\n'.join(self.DOCKER_HOSTS) + '\n')

    def tearDown(self):
        if self._saved_config:
            jarvis = Jarvis.get_instance()
            jarvis.save_config(self._saved_config)
            jarvis.config_dir = self._saved_config.get('config_dir', jarvis.config_dir)
            jarvis.private_dir = self._saved_config.get('private_dir', jarvis.private_dir)
            jarvis.shared_dir = self._saved_config.get('shared_dir', jarvis.shared_dir)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_redis_pkg_def(self, pipeline_name, port=6379):
        return {
            'pkg_type': 'builtin.redis',
            'pkg_id': 'redis',
            'pkg_name': 'redis',
            'global_id': f'{pipeline_name}.redis',
            'config': {
                'port': port,
                'sleep': 2,
                'interceptors': [],
            },
        }

    def _make_bench_pkg_def(self, pipeline_name, port=6379):
        return {
            'pkg_type': 'builtin.redis-benchmark',
            'pkg_id': 'redis_bench',
            'pkg_name': 'redis-benchmark',
            'global_id': f'{pipeline_name}.redis_bench',
            'config': {
                'port': port,
                'count': 100000,
                'write': True,
                'read': True,
                'nthreads': 4,
                'pipeline': 16,
                'req_size': 64,
                'node': 0,
                'interceptors': [],
            },
        }

    def _create_redis_pipeline(self, name):
        """Create a pipeline with Redis + redis-benchmark and the Docker hostfile."""
        pipeline = Pipeline()
        pipeline.create(name)
        pipeline.install_manager = 'container'
        pipeline.container_engine = 'docker'
        pipeline.container_base = 'ubuntu:24.04'

        hf = Hostfile(path=self.hostfile_path, find_ips=False)
        pipeline.hostfile = hf

        redis_def = self._make_redis_pkg_def(name)
        bench_def = self._make_bench_pkg_def(name)
        pipeline.packages.append(redis_def)
        pipeline.packages.append(bench_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()
        return pipeline

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_pipeline_has_two_packages(self):
        """Pipeline should contain exactly the redis service and benchmark."""
        pipeline = self._create_redis_pipeline('redis_two_pkgs')

        self.assertEqual(len(pipeline.packages), 2)
        self.assertEqual(pipeline.packages[0]['pkg_type'], 'builtin.redis')
        self.assertEqual(pipeline.packages[1]['pkg_type'], 'builtin.redis-benchmark')

    def test_pipeline_persists_across_save_load(self):
        """Both packages and the hostfile should survive save/load."""
        self._create_redis_pipeline('redis_persist')

        pipeline2 = Pipeline('redis_persist')
        self.assertEqual(len(pipeline2.packages), 2)
        self.assertEqual(pipeline2.packages[0]['pkg_id'], 'redis')
        self.assertEqual(pipeline2.packages[1]['pkg_id'], 'redis_bench')

        hf = pipeline2.get_hostfile()
        self.assertIsNotNone(hf)
        self.assertEqual(hf.hosts, self.DOCKER_HOSTS)

    def test_redis_inherits_pipeline_hostfile(self):
        """Redis package should fall back to the pipeline hostfile."""
        pipeline = self._create_redis_pipeline('redis_hf')
        redis_def = pipeline.packages[0]
        pkg = pipeline._load_package_instance(redis_def, {})

        pkg_hf = pkg.get_hostfile()
        self.assertEqual(len(pkg_hf.hosts), 4)
        self.assertEqual(pkg_hf.hosts, self.DOCKER_HOSTS)

    def test_benchmark_inherits_pipeline_hostfile(self):
        """Redis-benchmark should also inherit the pipeline hostfile."""
        pipeline = self._create_redis_pipeline('redis_bench_hf')
        bench_def = pipeline.packages[1]
        pkg = pipeline._load_package_instance(bench_def, {})

        pkg_hf = pkg.get_hostfile()
        self.assertEqual(len(pkg_hf.hosts), 4)
        self.assertEqual(pkg_hf.hosts, self.DOCKER_HOSTS)

    def test_hostfile_saved_into_shared_dir(self):
        """Hostfile must be copied into shared_dir so it is on the shared volume."""
        pipeline = self._create_redis_pipeline('redis_hf_shared')

        shared_dir = self.jarvis.get_pipeline_shared_dir('redis_hf_shared')
        expected_hf = str(shared_dir / 'hostfile')

        self.assertTrue(os.path.exists(expected_hf))
        saved_hf = Hostfile(path=expected_hf, find_ips=False)
        self.assertEqual(saved_hf.hosts, self.DOCKER_HOSTS)

        # pipeline.yaml should point to the shared_dir path
        config_file = self.jarvis.get_pipeline_dir('redis_hf_shared') / 'pipeline.yaml'
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f)
        self.assertEqual(config['hostfile'], expected_hf)

    def test_compose_mounts_at_same_path(self):
        """
        Shared and private dirs must be mounted at the same host path
        inside the container so that all config files (hostfile,
        redis.conf, pipeline.yaml) resolve identically.
        """
        pipeline = self._create_redis_pipeline('redis_compose')
        pipeline.container_image = 'redis-test:latest'
        pipeline.save()

        compose_path = pipeline._generate_pipeline_compose_file()
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)

        volumes = compose['services']['redis_compose']['volumes']

        shared_dir = str(self.jarvis.get_pipeline_shared_dir('redis_compose'))
        private_dir = str(self.jarvis.get_pipeline_private_dir('redis_compose'))

        self.assertIn(f'{shared_dir}:{shared_dir}', volumes)
        self.assertIn(f'{private_dir}:{private_dir}', volumes)

    def test_compose_container_runs_ssh_and_sleeps(self):
        """Container entrypoint should start SSH and sleep, not run jarvis."""
        pipeline = self._create_redis_pipeline('redis_cmd')
        pipeline.container_image = 'redis-test:latest'
        pipeline.save()

        compose_path = pipeline._generate_pipeline_compose_file()
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)

        cmd = compose['services']['redis_cmd']['command'][0]

        self.assertIn('/usr/sbin/sshd', cmd)
        self.assertIn('sleep infinity', cmd)
        self.assertNotIn('jarvis', cmd)

    def test_redis_conf_written_to_shared_dir(self):
        """
        Redis._configure copies redis.conf into shared_dir so it is
        visible inside the container via the shared volume mount.
        """
        pipeline = self._create_redis_pipeline('redis_conf')
        redis_def = pipeline.packages[0]
        pkg = pipeline._load_package_instance(redis_def, {})
        pkg.configure(**pkg.config)

        expected_conf = os.path.join(pkg.shared_dir, 'redis.conf')
        self.assertTrue(os.path.exists(expected_conf))

    def test_redis_cluster_host_string(self):
        """
        Redis.start() builds a cluster create command from the hostfile.
        Verify the expected host:port pairs match the 4-node hostfile.
        """
        pipeline = self._create_redis_pipeline('redis_cluster_str')
        redis_def = pipeline.packages[0]
        pkg = pipeline._load_package_instance(redis_def, {})

        hf = pkg.get_hostfile()
        port = pkg.config['port']
        host_str = ' '.join(f'{h}:{port}' for h in hf.hosts)

        expected = (
            'redis-node-01:6379 redis-node-02:6379 '
            'redis-node-03:6379 redis-node-04:6379'
        )
        self.assertEqual(host_str, expected)

    def test_benchmark_targets_first_node_by_default(self):
        """redis-benchmark should target hostfile[node] (node=0 by default)."""
        pipeline = self._create_redis_pipeline('redis_bench_node')
        bench_def = pipeline.packages[1]
        pkg = pipeline._load_package_instance(bench_def, {})

        hf = pkg.get_hostfile()
        target_host = hf.hosts[pkg.config['node']]
        self.assertEqual(target_host, 'redis-node-01')

    def test_install_manager_container(self):
        """Pipeline should have install_manager=container."""
        pipeline = self._create_redis_pipeline('redis_deploy')
        self.assertEqual(pipeline.install_manager, 'container')

        # Reload and verify persistence
        pipeline2 = Pipeline('redis_deploy')
        self.assertEqual(pipeline2.install_manager, 'container')


if __name__ == '__main__':
    unittest.main()
