"""
Integration tests for containerized packages in single-node (baremetal) mode.
Verifies that each container-capable package correctly:
  - Returns (dockerfile_content, image_suffix) from _build_phase / _build_deploy_phase
  - Reads Dockerfile templates from pkg_dir with ##VAR## substitution
  - Produces correct image names including the suffix
  - Handles deploy_mode='default' (returns None, skips container build)
"""
import unittest
import sys
import os
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.pipeline import Pipeline


def initialize_jarvis_for_test(config_dir, private_dir, shared_dir):
    jarvis = Jarvis.get_instance()
    jarvis.initialize(config_dir, private_dir, shared_dir, force=True)
    return jarvis


class ContainerBaremetalTestBase(unittest.TestCase):
    """Base class with shared setUp/tearDown for container baremetal tests."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_baremetal_')
        self.config_dir = os.path.join(self.test_dir, 'config')
        self.private_dir = os.path.join(self.test_dir, 'private')
        self.shared_dir = os.path.join(self.test_dir, 'shared')
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.private_dir, exist_ok=True)
        os.makedirs(self.shared_dir, exist_ok=True)

        os.environ['JARVIS_CONFIG'] = self.config_dir
        os.environ['JARVIS_PRIVATE'] = self.private_dir
        os.environ['JARVIS_SHARED'] = self.shared_dir

        self.jarvis = initialize_jarvis_for_test(
            self.config_dir, self.private_dir, self.shared_dir
        )

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def _create_pipeline(self, name, install_manager='container',
                         container_engine='docker',
                         container_base='ubuntu:24.04'):
        pipeline = Pipeline()
        pipeline.create(name)
        pipeline.install_manager = install_manager
        pipeline.container_engine = container_engine
        pipeline.container_base = container_base
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

    def _load_pkg(self, pipeline, pkg_def):
        pipeline.packages.append(pkg_def)
        pipeline._propagate_deploy_mode()
        pipeline.save()
        return pipeline._load_package_instance(pkg_def, {})


# =====================================================================
# IOR
# =====================================================================

class TestIorBaremetal(ContainerBaremetalTestBase):

    def test_default_mode_returns_none(self):
        pipeline = self._create_pipeline('ior_default', install_manager=None)
        pkg_def = self._make_pkg_def('ior_default', 'builtin.ior', 'ior', {
            'nprocs': 1, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        pkg = self._load_pkg(pipeline, pkg_def)
        self.assertIsNone(pkg._build_phase())
        self.assertIsNone(pkg._build_deploy_phase())

    def test_container_mode_returns_tuple(self):
        pipeline = self._create_pipeline('ior_container')
        pkg_def = self._make_pkg_def('ior_container', 'builtin.ior', 'ior', {

            'nprocs': 2, 'ppn': 2, 'block': '64m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': True, 'fpp': False, 'reps': 1, 'direct': False,
        })
        pkg = self._load_pkg(pipeline, pkg_def)

        result = pkg._build_phase()
        self.assertIsNotNone(result)
        content, suffix = result
        self.assertIsInstance(content, str)
        self.assertIsInstance(suffix, str)
        self.assertIn('set -e', content)
        self.assertNotIn('FROM', content)
        self.assertEqual(suffix, 'mpi')

    def test_build_image_name_includes_suffix(self):
        pipeline = self._create_pipeline('ior_name')
        pkg_def = self._make_pkg_def('ior_name', 'builtin.ior', 'ior', {

            'nprocs': 1, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        pkg = self._load_pkg(pipeline, pkg_def)
        _, suffix = pkg._build_phase()
        pkg._build_suffix = suffix
        self.assertEqual(pkg.build_image_name(), f'jarvis-build-ior-{suffix}')

    def test_dockerfile_template_substitution(self):
        pipeline = self._create_pipeline('ior_tpl')
        pkg_def = self._make_pkg_def('ior_tpl', 'builtin.ior', 'ior', {

            'nprocs': 1, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        pkg = self._load_pkg(pipeline, pkg_def)
        content, _ = pkg._build_phase()
        # build.sh should be a shell script, not a Dockerfile
        self.assertIn('set -e', content)
        self.assertNotIn('FROM', content)

    def test_deploy_phase_references_build_image(self):
        pipeline = self._create_pipeline('ior_deploy')
        pkg_def = self._make_pkg_def('ior_deploy', 'builtin.ior', 'ior', {

            'nprocs': 1, 'ppn': 1, 'block': '32m', 'xfer': '1m',
            'api': 'posix', 'out': '/tmp/ior.bin', 'write': True,
            'read': False, 'fpp': False, 'reps': 1, 'direct': False,
        })
        pkg = self._load_pkg(pipeline, pkg_def)
        _, build_suffix = pkg._build_phase()
        pkg._build_suffix = build_suffix
        content, deploy_suffix = pkg._build_deploy_phase()
        self.assertIn(pkg.build_image_name(), content)
        self.assertEqual(deploy_suffix, build_suffix)


# =====================================================================
# LAMMPS
# =====================================================================

class TestLammpsBaremetal(ContainerBaremetalTestBase):

    def _lammps_config(self, kokkos_gpu=True, cuda_arch=80):
        return {
            'nprocs': 4, 'ppn': 4, 'cuda_arch': cuda_arch,
            'base_image': 'sci-hpc-base', 'kokkos_gpu': kokkos_gpu,
            'num_gpus': 1, 'script': '/opt/lammps/bench/in.lj',
            'out': '/tmp/lammps_out', 'lmp_bin': 'lmp',
        }

    def test_default_mode_returns_none(self):
        pipeline = self._create_pipeline('lammps_def', install_manager=None)
        pkg_def = self._make_pkg_def('lammps_def', 'builtin.lammps', 'lammps',
                                     self._lammps_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        self.assertIsNone(pkg._build_phase())

    def test_gpu_suffix(self):
        pipeline = self._create_pipeline('lammps_gpu')
        pkg_def = self._make_pkg_def('lammps_gpu', 'builtin.lammps', 'lammps',
                                     self._lammps_config(kokkos_gpu=True, cuda_arch=80))
        pkg = self._load_pkg(pipeline, pkg_def)
        content, suffix = pkg._build_phase()
        self.assertEqual(suffix, 'kokkos-gpu-80')
        self.assertIn('KOKKOS', content)

    def test_cpu_suffix(self):
        pipeline = self._create_pipeline('lammps_cpu')
        pkg_def = self._make_pkg_def('lammps_cpu', 'builtin.lammps', 'lammps',
                                     self._lammps_config(kokkos_gpu=False))
        pkg = self._load_pkg(pipeline, pkg_def)
        content, suffix = pkg._build_phase()
        self.assertEqual(suffix, 'cpu')
        self.assertNotIn('KOKKOS', content)

    def test_different_arch_produces_different_suffix(self):
        pipeline = self._create_pipeline('lammps_arch')
        pkg_def = self._make_pkg_def('lammps_arch', 'builtin.lammps', 'lammps',
                                     self._lammps_config(cuda_arch=90))
        pkg = self._load_pkg(pipeline, pkg_def)
        _, suffix = pkg._build_phase()
        self.assertEqual(suffix, 'kokkos-gpu-90')
        self.assertIn('90', pkg.build_image_name(suffix))

    def test_deploy_phase_tuple(self):
        pipeline = self._create_pipeline('lammps_dpl', container_base='sci-hpc-base')
        pkg_def = self._make_pkg_def('lammps_dpl', 'builtin.lammps', 'lammps',
                                     self._lammps_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        _, build_suffix = pkg._build_phase()
        pkg._build_suffix = build_suffix
        content, deploy_suffix = pkg._build_deploy_phase()
        self.assertEqual(deploy_suffix, build_suffix)
        self.assertIn('lmp', content)


# =====================================================================
# Gray-Scott
# =====================================================================

class TestGrayScottBaremetal(ContainerBaremetalTestBase):

    def _gs_config(self, cuda_arch=80):
        return {
            'nprocs': 4, 'ppn': 4, 'cuda_arch': cuda_arch,
            'base_image': 'sci-hpc-base',
            'width': 256, 'height': 256, 'steps': 100, 'out_every': 50,
            'outdir': '/tmp/gs_out', 'F': 0.035, 'k': 0.060,
            'Du': 0.16, 'Dv': 0.08,
        }

    def test_default_mode_returns_none(self):
        pipeline = self._create_pipeline('gs_def', install_manager=None)
        pkg_def = self._make_pkg_def('gs_def', 'builtin.gray_scott', 'gray_scott',
                                     self._gs_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        self.assertIsNone(pkg._build_phase())

    def test_cuda_suffix(self):
        pipeline = self._create_pipeline('gs_cuda')
        pkg_def = self._make_pkg_def('gs_cuda', 'builtin.gray_scott', 'gray_scott',
                                     self._gs_config(cuda_arch=80))
        pkg = self._load_pkg(pipeline, pkg_def)
        content, suffix = pkg._build_phase()
        self.assertEqual(suffix, 'cuda-80')
        self.assertIn('CUDA_ARCH', content)

    def test_template_reads_from_pkg_dir(self):
        pipeline = self._create_pipeline('gs_tpl')
        pkg_def = self._make_pkg_def('gs_tpl', 'builtin.gray_scott', 'gray_scott',
                                     self._gs_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        content, _ = pkg._build_phase()
        self.assertIn('set -e', content)
        self.assertIn('gray_scott', content)


# =====================================================================
# AI Training
# =====================================================================

class TestAiTrainingBaremetal(ContainerBaremetalTestBase):

    def _ai_config(self):
        return {
            'script': '/opt/train_example.py', 'epochs': 3, 'batch': 128,
            'nnodes': 1, 'nproc_per_node': 1,
            'master_addr': 'localhost', 'master_port': 29500,
            'out': '/tmp/ai_out', 'base_image': 'sci-hpc-base',
        }

    def test_default_mode_returns_none(self):
        pipeline = self._create_pipeline('ai_def', install_manager=None)
        pkg_def = self._make_pkg_def('ai_def', 'builtin.ai_training', 'ai_training',
                                     self._ai_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        self.assertIsNone(pkg._build_phase())

    def test_suffix_is_pytorch(self):
        pipeline = self._create_pipeline('ai_cont')
        pkg_def = self._make_pkg_def('ai_cont', 'builtin.ai_training', 'ai_training',
                                     self._ai_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        content, suffix = pkg._build_phase()
        self.assertEqual(suffix, 'pytorch-cu126')
        self.assertIn('torch', content)

    def test_deploy_inherits_build_suffix(self):
        pipeline = self._create_pipeline('ai_dpl')
        pkg_def = self._make_pkg_def('ai_dpl', 'builtin.ai_training', 'ai_training',
                                     self._ai_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        _, build_suffix = pkg._build_phase()
        pkg._build_suffix = build_suffix
        _, deploy_suffix = pkg._build_deploy_phase()
        self.assertEqual(deploy_suffix, build_suffix)


# =====================================================================
# WarpX
# =====================================================================

class TestWarpxBaremetal(ContainerBaremetalTestBase):

    def _warpx_config(self, cuda_arch=80):
        return {
            'nprocs': 2, 'ppn': 2, 'cuda_arch': cuda_arch,
            'base_image': 'sci-hpc-base',
            'example': 'laser_acceleration', 'max_step': 10,
            'n_cell': '32 32 64', 'out': '/tmp/warpx_out',
            'plot_int': 5, 'inputs': None,
        }

    def test_default_mode_returns_none(self):
        pipeline = self._create_pipeline('warpx_def', install_manager=None)
        pkg_def = self._make_pkg_def('warpx_def', 'builtin.warpx', 'warpx',
                                     self._warpx_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        self.assertIsNone(pkg._build_phase())

    def test_cuda_suffix(self):
        pipeline = self._create_pipeline('warpx_cuda')
        pkg_def = self._make_pkg_def('warpx_cuda', 'builtin.warpx', 'warpx',
                                     self._warpx_config(cuda_arch=90))
        pkg = self._load_pkg(pipeline, pkg_def)
        content, suffix = pkg._build_phase()
        self.assertEqual(suffix, '3d-cuda-90')
        self.assertIn('WarpX', content)

    def test_deploy_phase_copies_binary(self):
        pipeline = self._create_pipeline('warpx_dpl')
        pkg_def = self._make_pkg_def('warpx_dpl', 'builtin.warpx', 'warpx',
                                     self._warpx_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        _, build_suffix = pkg._build_phase()
        pkg._build_suffix = build_suffix
        content, _ = pkg._build_deploy_phase()
        self.assertIn('COPY --from=builder', content)


# =====================================================================
# VPIC
# =====================================================================

class TestVpicBaremetal(ContainerBaremetalTestBase):

    def _vpic_config(self, base_image='sci-hpc-base', cuda_arch=80):
        return {
            'nprocs': 4, 'ppn': 4, 'cuda_arch': cuda_arch,
            'base_image': base_image, 'sample_deck': 'harris',
            'run_dir': '/tmp/vpic_run', 'deck': None,
        }

    def test_default_mode_returns_none(self):
        pipeline = self._create_pipeline('vpic_def', install_manager=None)
        pkg_def = self._make_pkg_def('vpic_def', 'builtin.vpic', 'vpic',
                                     self._vpic_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        self.assertIsNone(pkg._build_phase())

    def test_gpu_suffix(self):
        pipeline = self._create_pipeline('vpic_gpu')
        pkg_def = self._make_pkg_def('vpic_gpu', 'builtin.vpic', 'vpic',
                                     self._vpic_config(cuda_arch=80))
        pkg = self._load_pkg(pipeline, pkg_def)
        content, suffix = pkg._build_phase()
        self.assertEqual(suffix, 'kokkos-cuda-80')
        self.assertIn('ENABLE_KOKKOS_CUDA=ON', content)

    def test_cpu_suffix(self):
        pipeline = self._create_pipeline('vpic_cpu')
        pkg_def = self._make_pkg_def('vpic_cpu', 'builtin.vpic', 'vpic',
                                     self._vpic_config(base_image='ubuntu:24.04'))
        pkg = self._load_pkg(pipeline, pkg_def)
        content, suffix = pkg._build_phase()
        self.assertEqual(suffix, 'cpu')
        self.assertIn('ENABLE_KOKKOS_CUDA=OFF', content)

    def test_deploy_phase_nvcc_for_gpu(self):
        pipeline = self._create_pipeline('vpic_nvcc')
        pkg_def = self._make_pkg_def('vpic_nvcc', 'builtin.vpic', 'vpic',
                                     self._vpic_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        _, build_suffix = pkg._build_phase()
        pkg._build_suffix = build_suffix
        content, _ = pkg._build_deploy_phase()
        self.assertIn('nvcc_wrapper', content)


# =====================================================================
# Nyx
# =====================================================================

class TestNyxBaremetal(ContainerBaremetalTestBase):

    def _nyx_config(self, base_image='sci-hpc-base', cuda_arch=80):
        return {
            'nprocs': 4, 'ppn': 4, 'cuda_arch': cuda_arch,
            'base_image': base_image,
            'max_step': 10, 'n_cell': '64 64 64', 'max_level': 0,
            'out': '/tmp/nyx_out', 'plot_int': 5,
        }

    def test_default_mode_returns_none(self):
        pipeline = self._create_pipeline('nyx_def', install_manager=None)
        pkg_def = self._make_pkg_def('nyx_def', 'builtin.nyx', 'nyx',
                                     self._nyx_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        self.assertIsNone(pkg._build_phase())

    def test_gpu_suffix(self):
        pipeline = self._create_pipeline('nyx_gpu')
        pkg_def = self._make_pkg_def('nyx_gpu', 'builtin.nyx', 'nyx',
                                     self._nyx_config(cuda_arch=80))
        pkg = self._load_pkg(pipeline, pkg_def)
        content, suffix = pkg._build_phase()
        self.assertEqual(suffix, 'cuda-80')
        self.assertIn('GPU_BACKEND=CUDA', content)

    def test_cpu_suffix(self):
        pipeline = self._create_pipeline('nyx_cpu')
        pkg_def = self._make_pkg_def('nyx_cpu', 'builtin.nyx', 'nyx',
                                     self._nyx_config(base_image='ubuntu:24.04'))
        pkg = self._load_pkg(pipeline, pkg_def)
        content, suffix = pkg._build_phase()
        self.assertEqual(suffix, 'cpu')
        self.assertIn('GPU_BACKEND=NONE', content)

    def test_deploy_copies_hydrotests(self):
        pipeline = self._create_pipeline('nyx_dpl')
        pkg_def = self._make_pkg_def('nyx_dpl', 'builtin.nyx', 'nyx',
                                     self._nyx_config())
        pkg = self._load_pkg(pipeline, pkg_def)
        _, build_suffix = pkg._build_phase()
        pkg._build_suffix = build_suffix
        content, _ = pkg._build_deploy_phase()
        self.assertIn('nyx_HydroTests', content)


# =====================================================================
# Redis (deploy-only, no build phase)
# =====================================================================

class TestRedisBaremetal(ContainerBaremetalTestBase):

    def test_default_mode_returns_none(self):
        pipeline = self._create_pipeline('redis_def', install_manager=None)
        pkg_def = self._make_pkg_def('redis_def', 'builtin.redis', 'redis', {
            'port': 6379,
        })
        pkg = self._load_pkg(pipeline, pkg_def)
        self.assertIsNone(pkg._build_phase())
        self.assertIsNone(pkg._build_deploy_phase())

    def test_container_has_deploy_only(self):
        pipeline = self._create_pipeline('redis_cont')
        pkg_def = self._make_pkg_def('redis_cont', 'builtin.redis', 'redis', {
            'port': 6379,
        })
        pkg = self._load_pkg(pipeline, pkg_def)
        self.assertIsNone(pkg._build_phase())
        result = pkg._build_deploy_phase()
        self.assertIsNotNone(result)
        content, suffix = result
        self.assertIn('redis-server', content)
        self.assertEqual(suffix, 'default')


# =====================================================================
# Redis-Benchmark (deploy-only, no build phase)
# =====================================================================

class TestRedisBenchmarkBaremetal(ContainerBaremetalTestBase):

    def test_container_has_deploy_only(self):
        pipeline = self._create_pipeline('rbench_cont')
        pkg_def = self._make_pkg_def('rbench_cont', 'builtin.redis-benchmark',
                                     'redis_bench', {
            'port': 6379,
            'count': 1000, 'write': True, 'read': True,
            'nthreads': 1, 'pipeline': 1, 'req_size': 64, 'node': 0,
        })
        pkg = self._load_pkg(pipeline, pkg_def)
        self.assertIsNone(pkg._build_phase())
        result = pkg._build_deploy_phase()
        self.assertIsNotNone(result)
        content, suffix = result
        self.assertIn('redis', content)
        self.assertEqual(suffix, 'default')


# =====================================================================
# Cross-package: image name uniqueness
# =====================================================================

class TestImageNameUniqueness(ContainerBaremetalTestBase):
    """Different configurations of the same package produce distinct image names."""

    def test_lammps_gpu_vs_cpu_distinct(self):
        pipeline = self._create_pipeline('lammps_uniq')
        gpu_def = self._make_pkg_def('lammps_uniq', 'builtin.lammps', 'lammps_gpu', {
            'nprocs': 1, 'ppn': 1,
            'cuda_arch': 80, 'base_image': 'sci-hpc-base',
            'kokkos_gpu': True, 'num_gpus': 1,
            'script': '/opt/lammps/bench/in.lj', 'out': '/tmp/out',
            'lmp_bin': 'lmp',
        })
        cpu_def = self._make_pkg_def('lammps_uniq', 'builtin.lammps', 'lammps_cpu', {
            'nprocs': 1, 'ppn': 1,
            'cuda_arch': 80, 'base_image': 'sci-hpc-base',
            'kokkos_gpu': False, 'num_gpus': 0,
            'script': '/opt/lammps/bench/in.lj', 'out': '/tmp/out',
            'lmp_bin': 'lmp',
        })
        gpu_pkg = self._load_pkg(pipeline, gpu_def)
        _, gpu_suffix = gpu_pkg._build_phase()

        cpu_pkg = self._load_pkg(pipeline, cpu_def)
        _, cpu_suffix = cpu_pkg._build_phase()

        self.assertNotEqual(gpu_suffix, cpu_suffix)
        self.assertNotEqual(
            gpu_pkg.build_image_name(gpu_suffix),
            cpu_pkg.build_image_name(cpu_suffix),
        )

    def test_nyx_gpu_vs_cpu_distinct(self):
        pipeline = self._create_pipeline('nyx_uniq')
        gpu_def = self._make_pkg_def('nyx_uniq', 'builtin.nyx', 'nyx_gpu', {
            'nprocs': 1, 'ppn': 1,
            'cuda_arch': 80, 'base_image': 'sci-hpc-base',
            'max_step': 10, 'n_cell': '64 64 64', 'max_level': 0,
            'out': '/tmp/nyx_out', 'plot_int': 5,
        })
        cpu_def = self._make_pkg_def('nyx_uniq', 'builtin.nyx', 'nyx_cpu', {
            'nprocs': 1, 'ppn': 1,
            'cuda_arch': 80, 'base_image': 'ubuntu:24.04',
            'max_step': 10, 'n_cell': '64 64 64', 'max_level': 0,
            'out': '/tmp/nyx_out', 'plot_int': 5,
        })
        gpu_pkg = self._load_pkg(pipeline, gpu_def)
        _, gpu_suffix = gpu_pkg._build_phase()

        cpu_pkg = self._load_pkg(pipeline, cpu_def)
        _, cpu_suffix = cpu_pkg._build_phase()

        self.assertNotEqual(gpu_suffix, cpu_suffix)


if __name__ == '__main__':
    unittest.main()
