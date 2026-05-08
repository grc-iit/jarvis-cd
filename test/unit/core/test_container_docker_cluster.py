"""
Integration tests for containerized packages in Docker cluster mode.
Verifies that each container-capable package correctly:
  - Inherits hostfile from the pipeline
  - Generates docker-compose with shared/private mounts at same paths
  - Produces compose with SSH + sleep entrypoint
  - Stores hostfile in shared_dir for container visibility
  - Sets container_engine and container_base on the pipeline
"""
import unittest
import sys
import os
import tempfile
import shutil
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.pipeline import Pipeline
from jarvis_cd.util.hostfile import Hostfile


def initialize_jarvis_for_test(config_dir, private_dir, shared_dir):
    jarvis = Jarvis.get_instance()
    saved_config = None
    if jarvis.config_file.exists():
        with open(jarvis.config_file, 'r') as f:
            saved_config = yaml.safe_load(f)
    jarvis.initialize(config_dir, private_dir, shared_dir, force=False)
    return jarvis, saved_config


class DockerClusterTestBase(unittest.TestCase):
    """Base class with shared setUp/tearDown for Docker cluster tests."""

    DOCKER_HOSTS = ['node-01', 'node-02', 'node-03', 'node-04']

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_cluster_')
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

    def _create_cluster_pipeline(self, name, install_manager='container',
                                 container_engine='docker',
                                 container_base='ubuntu:24.04',
                                 container_image='test:latest'):
        pipeline = Pipeline()
        pipeline.create(name)
        pipeline.install_manager = install_manager
        pipeline.container_engine = container_engine
        pipeline.container_base = container_base
        pipeline.container_image = container_image
        hf = Hostfile(path=self.hostfile_path, find_ips=False)
        pipeline.hostfile = hf
        pipeline.save()
        return pipeline

    def _make_pkg_def(self, pipeline_name, pkg_type, pkg_name, config):
        config.setdefault('interceptors', [])
        return {
            'pkg_type': pkg_type,
            'pkg_id': pkg_name,
            'pkg_name': pkg_name.split('.')[-1],
            'global_id': f'{pipeline_name}.{pkg_name}',
            'config': config,
        }

    def _add_pkg(self, pipeline, pkg_def):
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()
        return pipeline._load_package_instance(pkg_def, {})


# =====================================================================
# IOR Docker Cluster
# =====================================================================

class TestIorDockerCluster(DockerClusterTestBase):

    def _ior_config(self):
        return {

            'nprocs': 4, 'ppn': 1, 'block': '64m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': True, 'fpp': False, 'reps': 1, 'direct': False,
        }

    def test_hostfile_four_nodes(self):
        hf = Hostfile(path=self.hostfile_path, find_ips=False)
        self.assertEqual(len(hf), 4)
        self.assertEqual(hf.hosts, self.DOCKER_HOSTS)

    def test_pkg_inherits_hostfile(self):
        pipeline = self._create_cluster_pipeline('ior_hf')
        pkg_def = self._make_pkg_def('ior_hf', 'builtin.ior', 'ior',
                                     self._ior_config())
        pkg = self._add_pkg(pipeline, pkg_def)
        pkg_hf = pkg.get_hostfile()
        self.assertEqual(len(pkg_hf.hosts), 4)

    def test_compose_mounts(self):
        pipeline = self._create_cluster_pipeline('ior_mnt')
        compose_path = pipeline._generate_pipeline_compose_file()
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)
        volumes = compose['services']['ior_mnt']['volumes']
        shared = str(self.jarvis.get_pipeline_shared_dir('ior_mnt'))
        private = str(self.jarvis.get_pipeline_private_dir('ior_mnt'))
        self.assertIn(f'{shared}:{shared}', volumes)
        self.assertIn(f'{private}:{private}', volumes)

    def test_compose_ssh_entrypoint(self):
        pipeline = self._create_cluster_pipeline('ior_ssh')
        compose_path = pipeline._generate_pipeline_compose_file()
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)
        cmd = compose['services']['ior_ssh']['command'][0]
        self.assertIn('/usr/sbin/sshd', cmd)
        self.assertIn('sleep infinity', cmd)

    def test_hostfile_in_shared_dir(self):
        pipeline = self._create_cluster_pipeline('ior_hfsd')
        shared = self.jarvis.get_pipeline_shared_dir('ior_hfsd')
        hf_path = str(shared / 'hostfile')
        self.assertTrue(os.path.exists(hf_path))
        saved_hf = Hostfile(path=hf_path, find_ips=False)
        self.assertEqual(saved_hf.hosts, self.DOCKER_HOSTS)

    def test_nprocs_matches_nodes(self):
        pipeline = self._create_cluster_pipeline('ior_np')
        pkg_def = self._make_pkg_def('ior_np', 'builtin.ior', 'ior',
                                     self._ior_config())
        pkg = self._add_pkg(pipeline, pkg_def)
        self.assertEqual(
            pkg.config['nprocs'] // pkg.config['ppn'],
            len(self.DOCKER_HOSTS),
        )


# =====================================================================
# LAMMPS Docker Cluster
# =====================================================================

class TestLammpsDockerCluster(DockerClusterTestBase):

    def _lammps_config(self):
        return {

            'nprocs': 4, 'ppn': 1, 'cuda_arch': 80,
            'base_image': 'sci-hpc-base', 'kokkos_gpu': False,
            'num_gpus': 0, 'script': '/opt/lammps/bench/in.lj',
            'out': '/tmp/lammps_out', 'lmp_bin': 'lmp',
        }

    def test_pkg_inherits_hostfile(self):
        pipeline = self._create_cluster_pipeline('lammps_hf')
        pkg_def = self._make_pkg_def('lammps_hf', 'builtin.lammps', 'lammps',
                                     self._lammps_config())
        pkg = self._add_pkg(pipeline, pkg_def)
        self.assertEqual(len(pkg.get_hostfile().hosts), 4)

    def test_compose_mounts(self):
        pipeline = self._create_cluster_pipeline('lammps_mnt')
        compose_path = pipeline._generate_pipeline_compose_file()
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)
        volumes = compose['services']['lammps_mnt']['volumes']
        shared = str(self.jarvis.get_pipeline_shared_dir('lammps_mnt'))
        self.assertIn(f'{shared}:{shared}', volumes)

    def test_container_engine_persists(self):
        self._create_cluster_pipeline('lammps_eng')
        pipeline2 = Pipeline('lammps_eng')
        self.assertEqual(pipeline2.container_engine, 'docker')


# =====================================================================
# Gray-Scott Docker Cluster
# =====================================================================

class TestGrayScottDockerCluster(DockerClusterTestBase):

    def _gs_config(self):
        return {

            'nprocs': 4, 'ppn': 1, 'cuda_arch': 80,
            'base_image': 'sci-hpc-base',
            'width': 256, 'height': 256, 'steps': 100, 'out_every': 50,
            'outdir': '/tmp/gs_out', 'F': 0.035, 'k': 0.060,
            'Du': 0.16, 'Dv': 0.08,
        }

    def test_pkg_inherits_hostfile(self):
        pipeline = self._create_cluster_pipeline('gs_hf')
        pkg_def = self._make_pkg_def('gs_hf', 'builtin.gray_scott',
                                     'gray_scott', self._gs_config())
        pkg = self._add_pkg(pipeline, pkg_def)
        self.assertEqual(len(pkg.get_hostfile().hosts), 4)

    def test_compose_ssh_entrypoint(self):
        pipeline = self._create_cluster_pipeline('gs_ssh')
        compose_path = pipeline._generate_pipeline_compose_file()
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)
        cmd = compose['services']['gs_ssh']['command'][0]
        self.assertIn('/usr/sbin/sshd', cmd)


# =====================================================================
# AI Training Docker Cluster
# =====================================================================

class TestAiTrainingDockerCluster(DockerClusterTestBase):

    def _ai_config(self):
        return {

            'script': '/opt/train_example.py', 'epochs': 3, 'batch': 128,
            'nnodes': 4, 'nproc_per_node': 1,
            'master_addr': 'node-01', 'master_port': 29500,
            'out': '/tmp/ai_out', 'base_image': 'sci-hpc-base',
        }

    def test_pkg_inherits_hostfile(self):
        pipeline = self._create_cluster_pipeline('ai_hf')
        pkg_def = self._make_pkg_def('ai_hf', 'builtin.ai_training',
                                     'ai_training', self._ai_config())
        pkg = self._add_pkg(pipeline, pkg_def)
        self.assertEqual(len(pkg.get_hostfile().hosts), 4)

    def test_compose_mounts(self):
        pipeline = self._create_cluster_pipeline('ai_mnt')
        compose_path = pipeline._generate_pipeline_compose_file()
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)
        volumes = compose['services']['ai_mnt']['volumes']
        shared = str(self.jarvis.get_pipeline_shared_dir('ai_mnt'))
        private = str(self.jarvis.get_pipeline_private_dir('ai_mnt'))
        self.assertIn(f'{shared}:{shared}', volumes)
        self.assertIn(f'{private}:{private}', volumes)


# =====================================================================
# WarpX Docker Cluster
# =====================================================================

class TestWarpxDockerCluster(DockerClusterTestBase):

    def _warpx_config(self):
        return {

            'nprocs': 4, 'ppn': 1, 'cuda_arch': 80,
            'base_image': 'sci-hpc-base',
            'example': 'laser_acceleration', 'max_step': 10,
            'n_cell': '32 32 64', 'out': '/tmp/warpx_out',
            'plot_int': 5, 'inputs': None,
        }

    def test_pkg_inherits_hostfile(self):
        pipeline = self._create_cluster_pipeline('warpx_hf')
        pkg_def = self._make_pkg_def('warpx_hf', 'builtin.warpx', 'warpx',
                                     self._warpx_config())
        pkg = self._add_pkg(pipeline, pkg_def)
        self.assertEqual(len(pkg.get_hostfile().hosts), 4)

    def test_compose_ssh_entrypoint(self):
        pipeline = self._create_cluster_pipeline('warpx_ssh')
        compose_path = pipeline._generate_pipeline_compose_file()
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)
        cmd = compose['services']['warpx_ssh']['command'][0]
        self.assertIn('sleep infinity', cmd)


# =====================================================================
# VPIC Docker Cluster
# =====================================================================

class TestVpicDockerCluster(DockerClusterTestBase):

    def _vpic_config(self):
        return {

            'nprocs': 4, 'ppn': 1, 'cuda_arch': 80,
            'base_image': 'sci-hpc-base', 'sample_deck': 'harris',
            'run_dir': '/tmp/vpic_run', 'deck': None,
        }

    def test_pkg_inherits_hostfile(self):
        pipeline = self._create_cluster_pipeline('vpic_hf')
        pkg_def = self._make_pkg_def('vpic_hf', 'builtin.vpic', 'vpic',
                                     self._vpic_config())
        pkg = self._add_pkg(pipeline, pkg_def)
        self.assertEqual(len(pkg.get_hostfile().hosts), 4)

    def test_compose_mounts(self):
        pipeline = self._create_cluster_pipeline('vpic_mnt')
        compose_path = pipeline._generate_pipeline_compose_file()
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)
        volumes = compose['services']['vpic_mnt']['volumes']
        shared = str(self.jarvis.get_pipeline_shared_dir('vpic_mnt'))
        self.assertIn(f'{shared}:{shared}', volumes)


# =====================================================================
# Nyx Docker Cluster
# =====================================================================

class TestNyxDockerCluster(DockerClusterTestBase):

    def _nyx_config(self):
        return {

            'nprocs': 4, 'ppn': 1, 'cuda_arch': 80,
            'base_image': 'sci-hpc-base',
            'max_step': 10, 'n_cell': '64 64 64', 'max_level': 0,
            'out': '/tmp/nyx_out', 'plot_int': 5,
        }

    def test_pkg_inherits_hostfile(self):
        pipeline = self._create_cluster_pipeline('nyx_hf')
        pkg_def = self._make_pkg_def('nyx_hf', 'builtin.nyx', 'nyx',
                                     self._nyx_config())
        pkg = self._add_pkg(pipeline, pkg_def)
        self.assertEqual(len(pkg.get_hostfile().hosts), 4)

    def test_compose_ssh_entrypoint(self):
        pipeline = self._create_cluster_pipeline('nyx_ssh')
        compose_path = pipeline._generate_pipeline_compose_file()
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)
        cmd = compose['services']['nyx_ssh']['command'][0]
        self.assertIn('/usr/sbin/sshd', cmd)


# =====================================================================
# Redis Docker Cluster
# =====================================================================

class TestRedisDockerCluster(DockerClusterTestBase):

    def test_two_packages_pipeline(self):
        pipeline = self._create_cluster_pipeline('redis_two')
        redis_def = self._make_pkg_def('redis_two', 'builtin.redis', 'redis', {
            'port': 6379, 'sleep': 2,
        })
        bench_def = self._make_pkg_def('redis_two', 'builtin.redis-benchmark',
                                       'redis_bench', {
            'port': 6379,
            'count': 1000, 'write': True, 'read': True,
            'nthreads': 1, 'pipeline': 1, 'req_size': 64, 'node': 0,
        })
        pipeline.packages.append(redis_def)
        pipeline.packages.append(bench_def)
        pipeline.save()
        self.assertEqual(len(pipeline.packages), 2)

    def test_redis_inherits_hostfile(self):
        pipeline = self._create_cluster_pipeline('redis_hf')
        redis_def = self._make_pkg_def('redis_hf', 'builtin.redis', 'redis', {
            'port': 6379, 'sleep': 2,
        })
        pkg = self._add_pkg(pipeline, redis_def)
        self.assertEqual(len(pkg.get_hostfile().hosts), 4)

    def test_compose_mounts(self):
        pipeline = self._create_cluster_pipeline('redis_mnt')
        compose_path = pipeline._generate_pipeline_compose_file()
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)
        volumes = compose['services']['redis_mnt']['volumes']
        shared = str(self.jarvis.get_pipeline_shared_dir('redis_mnt'))
        self.assertIn(f'{shared}:{shared}', volumes)

    def test_hostfile_in_shared_dir(self):
        self._create_cluster_pipeline('redis_hfsd')
        shared = self.jarvis.get_pipeline_shared_dir('redis_hfsd')
        hf_path = str(shared / 'hostfile')
        self.assertTrue(os.path.exists(hf_path))

    def test_pipeline_persists(self):
        pipeline = self._create_cluster_pipeline('redis_per')
        redis_def = self._make_pkg_def('redis_per', 'builtin.redis', 'redis', {
            'port': 6379, 'sleep': 2,
        })
        pipeline.packages.append(redis_def)
        pipeline.save()

        pipeline2 = Pipeline('redis_per')
        self.assertEqual(len(pipeline2.packages), 1)
        self.assertEqual(pipeline2.container_engine, 'docker')
        hf = pipeline2.get_hostfile()
        self.assertEqual(hf.hosts, self.DOCKER_HOSTS)


# =====================================================================
# Multi-package pipeline (IOR + Redis in one pipeline)
# =====================================================================

class TestMultiPackageCluster(DockerClusterTestBase):

    def test_multi_pkg_compose_one_service(self):
        """Pipeline with multiple packages should produce one compose service."""
        pipeline = self._create_cluster_pipeline('multi_pkg')
        ior_def = self._make_pkg_def('multi_pkg', 'builtin.ior', 'ior', {

            'nprocs': 4, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        redis_def = self._make_pkg_def('multi_pkg', 'builtin.redis', 'redis', {
            'port': 6379, 'sleep': 2,
        })
        pipeline.packages.append(ior_def)
        pipeline.packages.append(redis_def)
        pipeline.save()

        compose_path = pipeline._generate_pipeline_compose_file()
        with open(compose_path, 'r') as f:
            compose = yaml.safe_load(f)
        # Pipeline creates one service per pipeline name (shared container)
        self.assertIn('multi_pkg', compose['services'])

    def test_all_pkgs_inherit_same_hostfile(self):
        pipeline = self._create_cluster_pipeline('multi_hf')
        ior_def = self._make_pkg_def('multi_hf', 'builtin.ior', 'ior', {

            'nprocs': 4, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        redis_def = self._make_pkg_def('multi_hf', 'builtin.redis', 'redis', {
            'port': 6379, 'sleep': 2,
        })
        ior_pkg = self._add_pkg(pipeline, ior_def)
        redis_pkg = pipeline._load_package_instance(redis_def, {})

        self.assertEqual(ior_pkg.get_hostfile().hosts, self.DOCKER_HOSTS)
        self.assertEqual(redis_pkg.get_hostfile().hosts, self.DOCKER_HOSTS)


if __name__ == '__main__':
    unittest.main()
