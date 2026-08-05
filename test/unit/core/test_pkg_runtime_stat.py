"""Unit tests for the ``<pkg_id>.runtime`` pipeline-test statistic.

Regression coverage for a defect that shipped blank ``*.runtime`` columns in
every ``results.csv``: nine builtin packages reported the stat out of
``self.start_time``, an attribute *nothing in the codebase ever assigned*.

Two independent faults produced it, and both are covered here:

1. ``self.start_time`` was never set. ``ior``/``redis-benchmark`` read it via
   ``getattr(..., None)`` and quietly emitted an empty cell; the other seven
   (``fio``, ``filebench``, ``arldm``, ``gadget2``, ``cm1``, ``wfcommons``,
   ``ycsbc``) read it bare and raised ``AttributeError``. Because ``PipelineTest``
   wraps ``_get_stat`` in a warn-and-continue ``try/except``, that discarded
   *every* stat those packages would have reported, not just the runtime.
2. ``_get_stat`` runs on a **fresh** instance built by
   ``Pipeline._load_package_instance`` after the run, so even a correctly
   assigned attribute would not have survived from ``start()``.

The fix: ``Pipeline._timed_start`` measures each package's ``start()`` into
``Pipeline.pkg_runtimes``, and ``_load_package_instance`` replays it onto every
later instance of that ``pkg_id``.
"""
import importlib.util
import os
import pathlib
import tempfile
import time
import unittest

from jarvis_cd.core.pipeline import Pipeline
from jarvis_cd.core.pkg import Pkg

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _load_pkg_module(name, rel_path):
    spec = importlib.util.spec_from_file_location(
        name, str(_REPO_ROOT / rel_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ior_mod = _load_pkg_module('rt_ior_pkg', 'builtin/builtin/ior/pkg.py')
_rb_mod = _load_pkg_module(
    'rt_rb_pkg', 'builtin/builtin/redis-benchmark/pkg.py')
_fio_mod = _load_pkg_module('rt_fio_pkg', 'builtin/builtin/fio/pkg.py')


class _StubPkg:
    """Minimal stand-in for a package: records that start() ran."""

    def __init__(self, duration=0.0, boom=None):
        self.duration = duration
        self.boom = boom
        self.started = False
        self.start_time = None
        self.runtime = None

    def start(self):
        self.started = True
        if self.duration:
            time.sleep(self.duration)
        if self.boom:
            raise self.boom


class PkgTimingDefaultsTest(unittest.TestCase):
    """The base class must define the attributes, not just document them."""

    def test_base_pkg_defines_timing_attributes(self):
        pkg = Pkg(pipeline=None)
        # Defined -- so a _get_stat reading them on a package that never ran
        # emits an empty cell instead of killing the whole stat collection.
        self.assertIsNone(pkg.start_time)
        self.assertIsNone(pkg.runtime)

    def test_builtin_packages_read_runtime_not_start_time(self):
        # The bug was a name mismatch between what _get_stat read and what the
        # framework populated. Pin the corrected name across every builtin
        # that reports the stat.
        offenders = []
        for pkg_py in sorted(
                (_REPO_ROOT / 'builtin' / 'builtin').glob('*/pkg.py')):
            text = pkg_py.read_text()
            if '.runtime\'] = self.start_time' in text:
                offenders.append(str(pkg_py.relative_to(_REPO_ROOT)))
        self.assertEqual([], offenders)


class TimedStartTest(unittest.TestCase):
    """Pipeline.start() must measure each package and park the result."""

    def _pipeline(self):
        pipeline = object.__new__(Pipeline)
        pipeline.pkg_runtimes = {}
        return pipeline

    def test_records_runtime_on_the_pipeline(self):
        pipeline = self._pipeline()
        pkg = _StubPkg(duration=0.05)

        pipeline._timed_start({'pkg_id': 'cte_ior'}, pkg)

        self.assertTrue(pkg.started)
        self.assertIn('cte_ior', pipeline.pkg_runtimes)
        self.assertGreaterEqual(pipeline.pkg_runtimes['cte_ior'], 0.05)

    def test_sets_runtime_on_the_running_instance(self):
        pipeline = self._pipeline()
        pkg = _StubPkg()

        pipeline._timed_start({'pkg_id': 'redis_bench'}, pkg)

        self.assertIsNotNone(pkg.runtime)

    def test_never_populates_deprecated_start_time(self):
        # A package still reading self.start_time must get an empty cell, not
        # a wall-clock epoch filed under a column that means "seconds". This
        # is what keeps a half-deployed install -- updated framework, stale
        # builtin copy -- visibly blank rather than silently wrong.
        pipeline = self._pipeline()
        pkg = _StubPkg(duration=0.02)

        pipeline._timed_start({'pkg_id': 'cte_ior'}, pkg)

        self.assertIsNone(pkg.start_time)

    def test_records_runtime_even_when_start_raises(self):
        # A failed row still wants to show how long it burned before dying.
        pipeline = self._pipeline()
        pkg = _StubPkg(duration=0.02, boom=RuntimeError('ior aborted'))

        with self.assertRaises(RuntimeError):
            pipeline._timed_start({'pkg_id': 'nfs_ior'}, pkg)

        self.assertIn('nfs_ior', pipeline.pkg_runtimes)
        self.assertGreaterEqual(pipeline.pkg_runtimes['nfs_ior'], 0.02)

    def test_each_package_timed_separately(self):
        pipeline = self._pipeline()
        pipeline._timed_start({'pkg_id': 'fast'}, _StubPkg())
        pipeline._timed_start({'pkg_id': 'slow'}, _StubPkg(duration=0.05))

        self.assertLess(pipeline.pkg_runtimes['fast'],
                        pipeline.pkg_runtimes['slow'])


class GetStatRuntimeTest(unittest.TestCase):
    """_get_stat, on a fresh instance, must report the replayed runtime."""

    IOR_LOG = ('Max Write: 112.92 MiB/sec (118.40 MB/sec)\n'
               'Max Read:  256.10 MiB/sec (268.55 MB/sec)\n')

    def _tempfile(self, text, suffix='.log'):
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_ior_reports_replayed_runtime_alongside_bandwidth(self):
        pkg = object.__new__(_ior_mod.Ior)
        pkg.pkg_id = 'cte_ior'
        pkg.config = {'log': self._tempfile(self.IOR_LOG)}
        pkg.runtime = 12.5          # as replayed by _load_package_instance

        stats = {}
        pkg._get_stat(stats)

        self.assertEqual(12.5, stats['cte_ior.runtime'])
        # The bandwidth parse must still work -- runtime is additive, and the
        # two used to come from the same call that AttributeError'd away.
        self.assertEqual(112.92, stats['cte_ior.write_max_mibs'])
        self.assertEqual(256.10, stats['cte_ior.read_max_mibs'])

    def test_ior_runtime_blank_but_harmless_when_never_started(self):
        pkg = object.__new__(_ior_mod.Ior)
        pkg.pkg_id = 'cte_ior'
        pkg.config = {'log': self._tempfile(self.IOR_LOG)}
        pkg.runtime = None

        stats = {}
        pkg._get_stat(stats)   # must not raise

        self.assertIsNone(stats['cte_ior.runtime'])
        self.assertEqual(112.92, stats['cte_ior.write_max_mibs'])

    def test_redis_benchmark_reports_replayed_runtime(self):
        pkg = object.__new__(_rb_mod.RedisBenchmark)
        pkg.pkg_id = 'redis_bench'
        pkg.config = {}
        pkg.runtime = 3.25
        pkg._csv_path = lambda: '/nonexistent/redis.csv'

        stats = {}
        pkg._get_stat(stats)

        self.assertEqual(3.25, stats['redis_bench.runtime'])

    def test_fio_get_stat_no_longer_raises(self):
        # fio's _get_stat is *only* the runtime line, so the old bare
        # self.start_time read meant fio contributed zero stats to any
        # pipeline test -- silently, via the warn-and-continue handler.
        pkg = object.__new__(_fio_mod.Fio)
        pkg.pkg_id = 'fio_bench'
        pkg.runtime = 7.0

        stats = {}
        pkg._get_stat(stats)

        self.assertEqual(7.0, stats['fio_bench.runtime'])


if __name__ == '__main__':
    unittest.main()
