"""Unit tests for the #526 v3 builtin-package logic:

- ior's effective-hostfile resolution (num_nodes subset, single_instance
  collapse, container-mode path stripping)
- redis-benchmark's --csv output parsing
- juicefs's meta_use_head URL rewrite

These test the pure logic only; the MPI-in-container launch chain itself is
exercised on Ares by the smoke pipelines.
"""
import importlib.util
import os
import pathlib
import tempfile
import unittest

from jarvis_cd.util.hostfile import Hostfile

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _load_pkg_module(name, rel_path):
    spec = importlib.util.spec_from_file_location(
        name, str(_REPO_ROOT / rel_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ior_mod = _load_pkg_module('ior_pkg', 'builtin/builtin/ior/pkg.py')
_rb_mod = _load_pkg_module('rb_pkg', 'builtin/builtin/redis-benchmark/pkg.py')
_jfs_mod = _load_pkg_module('jfs_pkg', 'builtin/builtin/juicefs/pkg.py')


def _stub(cls, config, hostfile=None, engine='none'):
    """Instance of ``cls`` bypassing Pkg.__init__, with the hostfile /
    _container_engine properties shadowed by fixed values."""
    shadow = type('Stub' + cls.__name__, (cls,), {
        'hostfile': property(lambda self: self._stub_hf),
        '_container_engine': property(lambda self: self._stub_engine),
    })
    obj = object.__new__(shadow)
    obj.config = config
    obj._stub_hf = hostfile
    obj._stub_engine = engine
    return obj


class TestIorEffHostfile(unittest.TestCase):
    HOSTS = ['n1', 'n2', 'n3', 'n4']

    def _hf(self, path='/tmp/hf.txt'):
        hf = Hostfile(hosts=list(self.HOSTS), find_ips=False)
        hf.path = path
        return hf

    def test_defaults_are_noop(self):
        pkg = _stub(_ior_mod.Ior, {'num_nodes': 0, 'single_instance': False},
                    self._hf())
        hf = pkg._eff_hostfile()
        self.assertEqual(hf.hosts, self.HOSTS)
        self.assertEqual(hf.path, '/tmp/hf.txt')  # bare-metal keeps the file

    def test_num_nodes_subsets_first_n(self):
        pkg = _stub(_ior_mod.Ior, {'num_nodes': 2}, self._hf())
        self.assertEqual(pkg._eff_hostfile().hosts, ['n1', 'n2'])

    def test_single_instance_collapses_to_head(self):
        pkg = _stub(_ior_mod.Ior, {'single_instance': True}, self._hf())
        self.assertEqual(pkg._eff_hostfile().hosts, ['n1'])

    def test_single_instance_wins_over_num_nodes(self):
        pkg = _stub(_ior_mod.Ior,
                    {'num_nodes': 3, 'single_instance': True}, self._hf())
        self.assertEqual(pkg._eff_hostfile().hosts, ['n1'])

    def test_container_mode_strips_hostfile_path(self):
        pkg = _stub(_ior_mod.Ior, {'num_nodes': 0}, self._hf(),
                    engine='apptainer')
        hf = pkg._eff_hostfile()
        self.assertEqual(hf.hosts, self.HOSTS)
        self.assertIsNone(hf.path)  # forces an inline --host list

    def test_stonewall_flag_in_menu(self):
        names = [o['name'] for o in _ior_mod.Ior._configure_menu(
            object.__new__(_ior_mod.Ior))]
        for key in ('num_nodes', 'stonewall', 'single_instance'):
            self.assertIn(key, names)


class TestIorCompletionGate(unittest.TestCase):
    """_assert_ior_completed converts a silent MPI/ior failure (no summary
    in the log) into a failed combination instead of a false-green success."""

    WRITE_SUMMARY = (
        'access    bw(MiB/s)\n'
        'write     112.92     113.96     0.017 65536 1024.0 0.03 1.12 0.0 1.13 0\n'
        'Max Write: 112.92 MiB/sec (118.40 MB/sec)\n')
    # A hard multi-node abort: header/options only, no results block.
    ABORTED = ('PRTE has lost communication with a remote daemon.\n'
               'Host key verification failed.\n')

    def _pkg(self, config, log_text=None):
        pkg = object.__new__(_ior_mod.Ior)
        pkg.pkg_id = 'cte_ior'
        pkg.config = dict(config)
        if log_text is not None:
            fd, path = tempfile.mkstemp(suffix='.log')
            with os.fdopen(fd, 'w') as f:
                f.write(log_text)
            self.addCleanup(os.remove, path)
            pkg.config['log'] = path
        return pkg

    def test_passes_when_write_summary_present(self):
        pkg = self._pkg({'write': True, 'read': False}, self.WRITE_SUMMARY)
        pkg._assert_ior_completed()  # must not raise

    def test_raises_on_aborted_run(self):
        pkg = self._pkg({'write': True, 'read': False}, self.ABORTED)
        with self.assertRaises(RuntimeError):
            pkg._assert_ior_completed()

    def test_raises_when_log_missing(self):
        pkg = self._pkg({'write': True, 'read': False})
        pkg.config['log'] = '/nonexistent/ior.log'
        with self.assertRaises(RuntimeError):
            pkg._assert_ior_completed()

    def test_raises_when_read_requested_but_absent(self):
        # write-only summary but the combo also asked for a read phase.
        pkg = self._pkg({'write': True, 'read': True}, self.WRITE_SUMMARY)
        with self.assertRaises(RuntimeError):
            pkg._assert_ior_completed()

    def test_noop_when_no_workload_requested(self):
        pkg = self._pkg({'write': False, 'read': False}, self.ABORTED)
        pkg._assert_ior_completed()  # nothing requested -> nothing to validate


class TestRedisBenchmarkParse(unittest.TestCase):
    V7 = ('"test","rps","avg_latency_ms","min_latency_ms","p50_latency_ms",'
          '"p95_latency_ms","p99_latency_ms","max_latency_ms"\n'
          '"SET","95238.10","0.263","0.084","0.255","0.407","0.495","1.351"\n'
          '"GET","98039.22","0.256","0.084","0.247","0.399","0.479","1.079"\n')
    LEGACY = '"SET","95238.10"\n"GET","98039.22"\n'

    def _pkg(self):
        pkg = object.__new__(_rb_mod.RedisBenchmark)
        pkg.pkg_id = 'redis_bench'
        return pkg

    def test_v7_eight_column(self):
        stats = self._pkg()._parse_output(self.V7)
        self.assertEqual(stats['redis_bench.set_rps'], 95238.10)
        self.assertEqual(stats['redis_bench.get_rps'], 98039.22)
        self.assertEqual(stats['redis_bench.set_p50_ms'], 0.255)
        self.assertEqual(stats['redis_bench.get_p99_ms'], 0.479)

    def test_legacy_two_column(self):
        stats = self._pkg()._parse_output(self.LEGACY)
        self.assertEqual(stats['redis_bench.set_rps'], 95238.10)
        self.assertNotIn('redis_bench.set_p50_ms', stats)

    def test_junk_never_raises(self):
        self.assertEqual(
            self._pkg()._parse_output('redis: Connection refused\n'), {})
        self.assertEqual(self._pkg()._parse_output(''), {})

    def test_clients_flag_in_menu(self):
        names = [o['name'] for o in _rb_mod.RedisBenchmark._configure_menu(
            object.__new__(_rb_mod.RedisBenchmark))]
        self.assertIn('clients', names)
        self.assertIn('single_instance', names)


class TestJuicefsMetaUseHead(unittest.TestCase):
    def _pkg(self, meta_url, use_head=True, hosts=('head', 'n2')):
        hf = Hostfile(hosts=list(hosts), find_ips=False) if hosts else None
        return _stub(_jfs_mod.Juicefs,
                     {'meta_url': meta_url, 'meta_use_head': use_head}, hf)

    def test_rewrites_host_keeps_port_and_db(self):
        self.assertEqual(
            self._pkg('redis://127.0.0.1:6379/1')._effective_meta_url(),
            'redis://head:6379/1')

    def test_preserves_credentials(self):
        self.assertEqual(
            self._pkg('redis://:pw@127.0.0.1:6379/1')._effective_meta_url(),
            'redis://:pw@head:6379/1')

    def test_disabled_is_identity(self):
        self.assertEqual(
            self._pkg('redis://127.0.0.1:6379/1',
                      use_head=False)._effective_meta_url(),
            'redis://127.0.0.1:6379/1')

    def test_no_hostfile_is_identity(self):
        self.assertEqual(
            self._pkg('redis://127.0.0.1:6379/1',
                      hosts=None)._effective_meta_url(),
            'redis://127.0.0.1:6379/1')


if __name__ == '__main__':
    unittest.main()
