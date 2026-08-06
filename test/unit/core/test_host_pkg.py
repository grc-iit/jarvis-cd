"""
Unit tests for host_pkgs -- the declared host-baremetal prerequisites.

Covers:
  - parse(): normalization and strict validation of the YAML block
  - the backend registry (spack / pip / conda)
  - check_all(): missing packages raise with an actionable install hint
  - activation lands in os.environ, so plain subprocesses inherit it
  - probe memoization
  - Pipeline integration: load-time check, save/load round-trip,
    and the checks on run() / submit()
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.host_pkg import (
    HostPkg, HostPkgError, SpackHostPkg, PipHostPkg, CondaHostPkg,
    _parse_env0,
)
from jarvis_cd.core.pipeline import Pipeline


class FakeHostPkg(HostPkg):
    """Test backend: presence and activation are set by the test."""

    install_method = "faketest"

    present = True
    activation = {}

    def is_installed(self, query):
        return type(self).present

    def activate(self, query):
        return dict(type(self).activation)

    def install_hint(self, query):
        return f"fake-install {query}"


class HostPkgTestBase(unittest.TestCase):
    """Clears the probe cache around every test: it is process-global and
    would otherwise leak a previous test's verdict into the next one."""

    def setUp(self):
        HostPkg.clear_cache()
        FakeHostPkg.present = True
        FakeHostPkg.activation = {}
        self._saved_environ = dict(os.environ)

    def tearDown(self):
        HostPkg.clear_cache()
        os.environ.clear()
        os.environ.update(self._saved_environ)


class TestHostPkgParse(HostPkgTestBase):

    def test_none_and_empty_are_empty_list(self):
        self.assertEqual(HostPkg.parse(None), [])
        self.assertEqual(HostPkg.parse([]), [])

    def test_valid_entry_normalized(self):
        parsed = HostPkg.parse(
            [{'install_method': ' spack ', 'install_query': ' apptainer '}])
        self.assertEqual(
            parsed,
            [{'install_method': 'spack', 'install_query': 'apptainer'}])

    def test_multiple_entries_preserve_order(self):
        parsed = HostPkg.parse([
            {'install_method': 'spack', 'install_query': 'apptainer'},
            {'install_method': 'pip', 'install_query': 'pyyaml'},
        ])
        self.assertEqual([e['install_query'] for e in parsed],
                         ['apptainer', 'pyyaml'])

    def test_non_list_raises(self):
        with self.assertRaises(HostPkgError) as ctx:
            HostPkg.parse({'install_method': 'spack'})
        self.assertIn('must be a list', str(ctx.exception))

    def test_non_mapping_entry_raises(self):
        with self.assertRaises(HostPkgError) as ctx:
            HostPkg.parse(['apptainer'])
        self.assertIn('host_pkgs[0]', str(ctx.exception))

    def test_missing_install_query_raises(self):
        with self.assertRaises(HostPkgError) as ctx:
            HostPkg.parse([{'install_method': 'spack'}])
        self.assertIn('install_query', str(ctx.exception))

    def test_missing_install_method_raises(self):
        with self.assertRaises(HostPkgError) as ctx:
            HostPkg.parse([{'install_query': 'apptainer'}])
        self.assertIn('install_method', str(ctx.exception))

    def test_unknown_install_method_raises_naming_known(self):
        """A typo'd backend must fail at parse time, not silently skip the
        check and resurface as a missing binary mid-build."""
        with self.assertRaises(HostPkgError) as ctx:
            HostPkg.parse(
                [{'install_method': 'spak', 'install_query': 'apptainer'}])
        msg = str(ctx.exception)
        self.assertIn("'spak'", msg)
        self.assertIn('spack', msg)

    def test_unknown_key_raises(self):
        """Catches `install_qeury:`-style typos, which would otherwise
        parse as a missing install_query with a confusing message."""
        with self.assertRaises(HostPkgError) as ctx:
            HostPkg.parse([{'install_method': 'spack',
                            'install_query': 'apptainer',
                            'verison': '1.3'}])
        self.assertIn('verison', str(ctx.exception))


class TestHostPkgRegistry(HostPkgTestBase):

    def test_builtin_backends_registered(self):
        reg = HostPkg._registry()
        self.assertIs(reg['spack'], SpackHostPkg)
        self.assertIs(reg['pip'], PipHostPkg)
        self.assertIs(reg['conda'], CondaHostPkg)

    def test_for_method_unknown_raises(self):
        with self.assertRaises(HostPkgError):
            HostPkg.for_method('nope')

    def test_subclass_registers_itself(self):
        self.assertIsInstance(HostPkg.for_method('faketest'), FakeHostPkg)


class TestHostPkgCheckAll(HostPkgTestBase):

    def test_empty_is_noop(self):
        self.assertEqual(HostPkg.check_all([]), {})

    def test_missing_raises_with_install_hint(self):
        FakeHostPkg.present = False
        with self.assertRaises(HostPkgError) as ctx:
            HostPkg.check_all(
                [{'install_method': 'faketest', 'install_query': 'apptainer'}],
                context='my_pipeline')
        msg = str(ctx.exception)
        self.assertIn('my_pipeline', msg)
        self.assertIn('apptainer', msg)
        self.assertIn('fake-install apptainer', msg)

    def test_all_missing_reported_not_just_first(self):
        """A user fixing prerequisites one error at a time is a bad loop;
        report every missing package in one pass."""
        FakeHostPkg.present = False
        with self.assertRaises(HostPkgError) as ctx:
            HostPkg.check_all([
                {'install_method': 'faketest', 'install_query': 'aaa'},
                {'install_method': 'faketest', 'install_query': 'bbb'},
            ])
        msg = str(ctx.exception)
        self.assertIn('Missing 2 required host package', msg)
        self.assertIn('aaa', msg)
        self.assertIn('bbb', msg)

    def test_present_activates_into_os_environ(self):
        """The activation has to reach os.environ: the container-build
        subprocesses run under a bare LocalExecInfo() and therefore see
        an unmodified process environment."""
        FakeHostPkg.activation = {'HOSTPKG_TEST_VAR': '/opt/fake/bin'}
        merged = HostPkg.check_all(
            [{'install_method': 'faketest', 'install_query': 'x'}])
        self.assertEqual(merged, {'HOSTPKG_TEST_VAR': '/opt/fake/bin'})
        self.assertEqual(os.environ['HOSTPKG_TEST_VAR'], '/opt/fake/bin')

    def test_activation_visible_to_subprocess(self):
        """End-to-end version of the above: a plain subprocess -- what
        LocalExec.run spawns -- must observe the activated variable."""
        import subprocess
        FakeHostPkg.activation = {'HOSTPKG_SUBPROC_VAR': 'activated'}
        HostPkg.check_all(
            [{'install_method': 'faketest', 'install_query': 'x'}])
        out = subprocess.run(
            ['bash', '-c', 'echo -n "$HOSTPKG_SUBPROC_VAR"'],
            capture_output=True, text=True)
        self.assertEqual(out.stdout, 'activated')

    def test_present_also_merges_into_pipeline_env(self):
        FakeHostPkg.activation = {'HOSTPKG_TEST_VAR': 'v'}
        env = {}
        HostPkg.check_all(
            [{'install_method': 'faketest', 'install_query': 'x'}], env=env)
        self.assertEqual(env['HOSTPKG_TEST_VAR'], 'v')

    def test_nothing_applied_when_any_package_missing(self):
        """Partial activation would leave the process half-configured
        after a failed check."""
        FakeHostPkg.present = False
        env = {}
        with self.assertRaises(HostPkgError):
            HostPkg.check_all(
                [{'install_method': 'faketest', 'install_query': 'x'}],
                env=env)
        self.assertEqual(env, {})

    def test_probe_is_memoized(self):
        """A sweep re-loads the same YAML once per combination; the probe
        subprocess must not re-run each time."""
        calls = []

        def counting_is_installed(self, query):
            calls.append(query)
            return True

        with patch.object(FakeHostPkg, 'is_installed', counting_is_installed):
            spec = [{'install_method': 'faketest', 'install_query': 'x'}]
            HostPkg.check_all(spec)
            HostPkg.check_all(spec)
            HostPkg.check_all(spec)
        self.assertEqual(len(calls), 1)

    def test_clear_cache_forces_reprobe(self):
        calls = []

        def counting_is_installed(self, query):
            calls.append(query)
            return True

        with patch.object(FakeHostPkg, 'is_installed', counting_is_installed):
            spec = [{'install_method': 'faketest', 'install_query': 'x'}]
            HostPkg.check_all(spec)
            HostPkg.clear_cache()
            HostPkg.check_all(spec)
        self.assertEqual(len(calls), 2)


class TestEnvParsing(HostPkgTestBase):
    """`env -0` parsing. Exported bash functions are the reason this is
    NUL-separated and identifier-filtered rather than a line split."""

    def test_parses_nul_separated_pairs(self):
        parsed = _parse_env0('A=1\0B=two\0')
        self.assertEqual(parsed, {'A': '1', 'B': 'two'})

    def test_value_with_newline_survives(self):
        parsed = _parse_env0('A=line1\nline2\0B=2\0')
        self.assertEqual(parsed['A'], 'line1\nline2')
        self.assertEqual(parsed['B'], '2')

    def test_exported_bash_function_dropped(self):
        """`BASH_FUNC_spack%%=() { ... }` is not a variable a child process
        should inherit, and its body would otherwise parse as junk keys."""
        parsed = _parse_env0(
            'BASH_FUNC_spack%%=() {  eval spack\n}\0PATH=/usr/bin\0')
        self.assertEqual(parsed, {'PATH': '/usr/bin'})

    def test_shell_noise_dropped(self):
        parsed = _parse_env0('_=/usr/bin/env\0SHLVL=3\0PWD=/tmp\0PATH=/bin\0')
        self.assertEqual(parsed, {'PATH': '/bin'})


class TestSpackHostPkg(HostPkgTestBase):

    def test_install_hint(self):
        self.assertEqual(
            SpackHostPkg().install_hint('apptainer'),
            'spack install apptainer')

    def test_not_installed_when_spack_absent(self):
        """The CI case: no spack binary and no SPACK_ROOT at all."""
        os.environ.pop('SPACK_ROOT', None)
        with patch('jarvis_cd.core.host_pkg.shutil.which', return_value=None):
            self.assertFalse(SpackHostPkg().is_installed('apptainer'))

    def test_activate_returns_only_changed_vars(self):
        """An activation carries what `spack load` changed -- not a copy of
        the whole host environment."""
        os.environ['HOSTPKG_UNCHANGED'] = 'same'
        fake = type('R', (), {
            'returncode': 0,
            'stdout': 'HOSTPKG_UNCHANGED=same\0HOSTPKG_NEW=/opt/x/bin\0',
        })()
        with patch('jarvis_cd.core.host_pkg._run_capture', return_value=fake):
            changed = SpackHostPkg().activate('apptainer')
        self.assertEqual(changed, {'HOSTPKG_NEW': '/opt/x/bin'})

    def test_activate_empty_on_failure(self):
        fake = type('R', (), {'returncode': 1, 'stdout': ''})()
        with patch('jarvis_cd.core.host_pkg._run_capture', return_value=fake):
            self.assertEqual(SpackHostPkg().activate('apptainer'), {})


class TestPipHostPkg(HostPkgTestBase):

    def test_dist_name_strips_version_specifiers(self):
        self.assertEqual(PipHostPkg._dist_name('pyyaml'), 'pyyaml')
        self.assertEqual(PipHostPkg._dist_name('pyyaml>=6.0'), 'pyyaml')
        self.assertEqual(PipHostPkg._dist_name('ruamel.yaml==0.17.1'),
                         'ruamel.yaml')
        self.assertEqual(PipHostPkg._dist_name('requests[socks]'), 'requests')

    def test_install_hint(self):
        self.assertEqual(PipHostPkg().install_hint('pyyaml>=6'),
                         'python3 -m pip install pyyaml>=6')

    def test_installed_dependency_detected(self):
        """pyyaml is a hard jarvis dependency, so it is present wherever
        these tests run."""
        self.assertTrue(PipHostPkg().is_installed('pyyaml'))

    def test_absent_package_not_detected(self):
        self.assertFalse(
            PipHostPkg().is_installed('jarvis-definitely-not-real-pkg'))


class TestCondaHostPkg(HostPkgTestBase):

    def test_install_hint(self):
        self.assertEqual(CondaHostPkg().install_hint('numpy'),
                         'conda install -y numpy')

    def test_not_installed_when_conda_absent(self):
        with patch('jarvis_cd.core.host_pkg.shutil.which', return_value=None):
            self.assertFalse(CondaHostPkg().is_installed('numpy'))

    def test_matches_exact_row_not_substring(self):
        """`conda list numpy` also lists numpy-base; only an exact name
        match counts."""
        fake = type('R', (), {
            'returncode': 0,
            'stdout': '# packages\nnumpy-base 1.0 py311\n',
        })()
        with patch('jarvis_cd.core.host_pkg.shutil.which',
                   return_value='/usr/bin/conda'):
            with patch('jarvis_cd.core.host_pkg._run_capture',
                       return_value=fake):
                self.assertFalse(CondaHostPkg().is_installed('numpy'))


class TestPipelineHostPkgs(HostPkgTestBase):
    """Pipeline-level integration: load, persist, run, submit."""

    def setUp(self):
        super().setUp()
        self.test_dir = tempfile.mkdtemp(
            prefix='jarvis_test_host_pkgs_', dir=os.path.expanduser('~'))
        self.config_dir = os.path.join(self.test_dir, 'config')
        self.private_dir = os.path.join(self.test_dir, 'private')
        self.shared_dir = os.path.join(self.test_dir, 'shared')
        for d in (self.config_dir, self.private_dir, self.shared_dir):
            os.makedirs(d, exist_ok=True)

        jarvis = Jarvis.get_instance()
        self._saved_config = None
        if jarvis.config_file.exists():
            with open(jarvis.config_file, 'r') as f:
                self._saved_config = yaml.safe_load(f)
        jarvis.initialize(self.config_dir, self.private_dir, self.shared_dir,
                          force=False)
        self.jarvis = jarvis

    def tearDown(self):
        # Restore the developer's real jarvis config; initialize() above
        # rewrites it in place.
        if self._saved_config:
            jarvis = Jarvis.get_instance()
            jarvis.save_config(self._saved_config)
            jarvis.config_dir = self._saved_config.get(
                'config_dir', jarvis.config_dir)
            jarvis.private_dir = self._saved_config.get(
                'private_dir', jarvis.private_dir)
            jarvis.shared_dir = self._saved_config.get(
                'shared_dir', jarvis.shared_dir)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        super().tearDown()

    def _write_yaml(self, name, body):
        path = os.path.join(self.test_dir, f'{name}.yaml')
        with open(path, 'w') as f:
            yaml.dump(body, f)
        return path

    def test_load_fails_fast_on_missing_host_pkg(self):
        FakeHostPkg.present = False
        path = self._write_yaml('hp_missing', {
            'name': 'hp_missing',
            'host_pkgs': [{'install_method': 'faketest',
                           'install_query': 'apptainer'}],
            'pkgs': [],
        })
        with self.assertRaises(HostPkgError) as ctx:
            Pipeline().load('yaml', path)
        self.assertIn('fake-install apptainer', str(ctx.exception))

    def test_load_fails_before_packages_are_processed(self):
        """The whole point of declaring a prerequisite is stopping before
        jarvis does work it cannot finish."""
        FakeHostPkg.present = False
        path = self._write_yaml('hp_early', {
            'name': 'hp_early',
            'host_pkgs': [{'install_method': 'faketest',
                           'install_query': 'apptainer'}],
            'pkgs': [{'pkg_type': 'builtin.ior', 'pkg_name': 'ior'}],
        })
        with patch.object(Pipeline, '_process_package_definition') as proc:
            with self.assertRaises(HostPkgError):
                Pipeline().load('yaml', path)
        proc.assert_not_called()

    def test_load_succeeds_and_activates_when_present(self):
        FakeHostPkg.activation = {'HOSTPKG_PPL_VAR': '/opt/apptainer/bin'}
        path = self._write_yaml('hp_ok', {
            'name': 'hp_ok',
            'host_pkgs': [{'install_method': 'faketest',
                           'install_query': 'apptainer'}],
            'pkgs': [],
        })
        ppl = Pipeline()
        ppl.load('yaml', path)
        self.assertEqual(
            ppl.host_pkgs,
            [{'install_method': 'faketest', 'install_query': 'apptainer'}])
        self.assertEqual(os.environ['HOSTPKG_PPL_VAR'], '/opt/apptainer/bin')
        self.assertEqual(ppl.env['HOSTPKG_PPL_VAR'], '/opt/apptainer/bin')

    def test_host_pkgs_round_trip_through_saved_config(self):
        """`jarvis ppl submit` against the current pipeline never re-reads
        the source YAML, so the declaration has to survive save()."""
        path = self._write_yaml('hp_rt', {
            'name': 'hp_rt',
            'host_pkgs': [{'install_method': 'faketest',
                           'install_query': 'apptainer'}],
            'pkgs': [],
        })
        Pipeline().load('yaml', path)

        reloaded = Pipeline('hp_rt')
        self.assertEqual(
            reloaded.host_pkgs,
            [{'install_method': 'faketest', 'install_query': 'apptainer'}])

    def test_no_host_pkgs_key_is_empty(self):
        path = self._write_yaml('hp_none', {'name': 'hp_none', 'pkgs': []})
        ppl = Pipeline()
        ppl.load('yaml', path)
        self.assertEqual(ppl.host_pkgs, [])

    def test_invalid_host_pkgs_block_rejected_at_load(self):
        path = self._write_yaml('hp_bad', {
            'name': 'hp_bad',
            'host_pkgs': [{'install_method': 'faketest'}],
            'pkgs': [],
        })
        with self.assertRaises(HostPkgError):
            Pipeline().load('yaml', path)

    def test_run_rechecks_saved_pipeline(self):
        """`jarvis ppl run` on the current pipeline skips _load_from_file,
        so run() has to check too."""
        path = self._write_yaml('hp_run', {
            'name': 'hp_run',
            'host_pkgs': [{'install_method': 'faketest',
                           'install_query': 'apptainer'}],
            'pkgs': [],
        })
        Pipeline().load('yaml', path)

        reloaded = Pipeline('hp_run')
        HostPkg.clear_cache()
        FakeHostPkg.present = False
        with self.assertRaises(HostPkgError):
            reloaded.run()

    def test_run_does_not_attempt_teardown_on_missing_host_pkg(self):
        """The check precedes the try/except, so a missing prerequisite
        does not drag the user through a stop() recovery trace."""
        path = self._write_yaml('hp_noteardown', {
            'name': 'hp_noteardown',
            'host_pkgs': [{'install_method': 'faketest',
                           'install_query': 'apptainer'}],
            'pkgs': [],
        })
        Pipeline().load('yaml', path)

        reloaded = Pipeline('hp_noteardown')
        HostPkg.clear_cache()
        FakeHostPkg.present = False
        with patch.object(Pipeline, 'stop') as stop:
            with self.assertRaises(HostPkgError):
                reloaded.run()
        stop.assert_not_called()

    def test_submit_checks_before_queueing(self):
        """Queueing a job that is guaranteed to fail wastes an allocation
        and the queue wait."""
        path = self._write_yaml('hp_submit', {
            'name': 'hp_submit',
            'host_pkgs': [{'install_method': 'faketest',
                           'install_query': 'apptainer'}],
            'scheduler': {'type': 'slurm', 'name': 'hp_submit',
                          'nnodes': 1, 'time': '00:10:00'},
            'pkgs': [],
        })
        Pipeline().load('yaml', path)

        reloaded = Pipeline('hp_submit')
        HostPkg.clear_cache()
        FakeHostPkg.present = False
        with patch('jarvis_cd.core.scheduler.make_scheduler') as mk:
            with self.assertRaises(HostPkgError):
                reloaded.submit(submit=True)
        mk.assert_not_called()


if __name__ == '__main__':
    unittest.main()
