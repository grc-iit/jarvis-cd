"""
Tests for pipeline test functionality (grid search experiments).
"""
import os
import csv
import sys
import yaml
import tempfile
import shutil
import unittest
from pathlib import Path

# Add the project root to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core.pipeline_test import (
    is_pipeline_test,
    PipelineTest,
    load_yaml_auto,
)


class TestIsPipelineTest(unittest.TestCase):
    """Tests for the is_pipeline_test() detection function."""

    def test_regular_pipeline_with_name_and_pkgs(self):
        """Regular pipeline with name and pkgs should return False."""
        yaml_data = {
            'name': 'my_pipeline',
            'pkgs': [
                {'pkg_type': 'builtin.ior', 'pkg_name': 'ior'}
            ]
        }
        self.assertFalse(is_pipeline_test(yaml_data))

    def test_regular_pipeline_with_name_only(self):
        """Regular pipeline with only name should return False."""
        yaml_data = {
            'name': 'my_pipeline'
        }
        self.assertFalse(is_pipeline_test(yaml_data))

    def test_regular_pipeline_with_pkgs_only(self):
        """Regular pipeline with only pkgs should return False."""
        yaml_data = {
            'pkgs': [
                {'pkg_type': 'builtin.ior', 'pkg_name': 'ior'}
            ]
        }
        self.assertFalse(is_pipeline_test(yaml_data))

    def test_pipeline_test_with_config_and_vars(self):
        """Pipeline test with config and vars should return True."""
        yaml_data = {
            'config': {
                'name': 'test',
                'pkgs': []
            },
            'vars': {
                'pkg.param': [1, 2, 3]
            }
        }
        self.assertTrue(is_pipeline_test(yaml_data))

    def test_pipeline_test_with_config_and_loop(self):
        """Pipeline test with config and loop should return True."""
        yaml_data = {
            'config': {
                'name': 'test',
                'pkgs': []
            },
            'loop': [['pkg.param']]
        }
        self.assertTrue(is_pipeline_test(yaml_data))

    def test_pipeline_test_with_config_and_repeat(self):
        """Pipeline test with config and repeat should return True."""
        yaml_data = {
            'config': {
                'name': 'test',
                'pkgs': []
            },
            'repeat': 5
        }
        self.assertTrue(is_pipeline_test(yaml_data))

    def test_pipeline_test_with_config_and_output(self):
        """Pipeline test with config and output should return True."""
        yaml_data = {
            'config': {
                'name': 'test',
                'pkgs': []
            },
            'output': '/tmp/results'
        }
        self.assertTrue(is_pipeline_test(yaml_data))

    def test_pipeline_test_full_format(self):
        """Full pipeline test format should return True."""
        yaml_data = {
            'config': {
                'name': 'full_test',
                'env': 'my_env',
                'pkgs': [
                    {'pkg_type': 'builtin.ior', 'pkg_name': 'ior', 'nprocs': 4}
                ]
            },
            'vars': {
                'ior.nprocs': [1, 2, 4, 8]
            },
            'loop': [['ior.nprocs']],
            'repeat': 3,
            'output': '/tmp/results'
        }
        self.assertTrue(is_pipeline_test(yaml_data))

    def test_config_only_is_test(self):
        """Config section alone (no test fields) should return True."""
        yaml_data = {
            'config': {
                'name': 'test',
                'pkgs': []
            }
        }
        # Config without any test fields - defaults to True since config is present
        self.assertTrue(is_pipeline_test(yaml_data))


class TestPipelineTestCombinationBuilding(unittest.TestCase):
    """Tests for PipelineTest combination building logic."""

    def test_no_vars_single_combination(self):
        """No variables should produce single empty combination."""
        test = PipelineTest()
        test.vars = {}
        test.loop = []
        test._build_combinations()

        self.assertEqual(len(test.combinations), 1)
        self.assertEqual(test.combinations[0], {})

    def test_single_variable_no_loop(self):
        """Single variable without explicit loop should iterate independently."""
        test = PipelineTest()
        test.vars = {
            'pkg.param': [1, 2, 3]
        }
        test.loop = []
        test._build_combinations()

        self.assertEqual(len(test.combinations), 3)
        self.assertEqual(test.combinations[0], {'pkg.param': 1})
        self.assertEqual(test.combinations[1], {'pkg.param': 2})
        self.assertEqual(test.combinations[2], {'pkg.param': 3})

    def test_two_independent_variables(self):
        """Two independent variables should create cartesian product."""
        test = PipelineTest()
        test.vars = {
            'a.x': [1, 2],
            'b.y': [10, 20]
        }
        test.loop = [['a.x'], ['b.y']]
        test._build_combinations()

        self.assertEqual(len(test.combinations), 4)
        # Should have all 4 combinations
        expected = [
            {'a.x': 1, 'b.y': 10},
            {'a.x': 1, 'b.y': 20},
            {'a.x': 2, 'b.y': 10},
            {'a.x': 2, 'b.y': 20},
        ]
        for exp in expected:
            self.assertIn(exp, test.combinations)

    def test_zipped_variables(self):
        """Variables in same loop group should be zipped together."""
        test = PipelineTest()
        test.vars = {
            'a.x': [1, 2, 3],
            'a.y': [10, 20, 30]
        }
        test.loop = [['a.x', 'a.y']]
        test._build_combinations()

        self.assertEqual(len(test.combinations), 3)
        self.assertEqual(test.combinations[0], {'a.x': 1, 'a.y': 10})
        self.assertEqual(test.combinations[1], {'a.x': 2, 'a.y': 20})
        self.assertEqual(test.combinations[2], {'a.x': 3, 'a.y': 30})

    def test_zipped_and_independent_variables(self):
        """Mixed zipped and independent variables."""
        test = PipelineTest()
        test.vars = {
            'a.x': [1, 2, 3],
            'a.y': [10, 20, 30],
            'b.z': [100, 200]
        }
        test.loop = [['a.x', 'a.y'], ['b.z']]
        test._build_combinations()

        # 3 zipped combinations * 2 independent = 6 total
        self.assertEqual(len(test.combinations), 6)

        # Verify some specific combinations
        self.assertIn({'a.x': 1, 'a.y': 10, 'b.z': 100}, test.combinations)
        self.assertIn({'a.x': 2, 'a.y': 20, 'b.z': 200}, test.combinations)
        self.assertIn({'a.x': 3, 'a.y': 30, 'b.z': 100}, test.combinations)

    def test_mismatched_zip_lengths_raises_error(self):
        """Zipped variables with different lengths should raise ValueError."""
        test = PipelineTest()
        test.vars = {
            'a.x': [1, 2, 3],
            'a.y': [10, 20]  # Different length!
        }
        test.loop = [['a.x', 'a.y']]

        with self.assertRaises(ValueError) as ctx:
            test._build_combinations()

        self.assertIn('must have the same number of values', str(ctx.exception))

    def test_undefined_variable_in_loop_raises_error(self):
        """Variable in loop not defined in vars should raise ValueError."""
        test = PipelineTest()
        test.vars = {
            'a.x': [1, 2, 3]
        }
        test.loop = [['a.x', 'a.undefined']]  # a.undefined not in vars

        with self.assertRaises(ValueError) as ctx:
            test._build_combinations()

        self.assertIn('not defined in vars section', str(ctx.exception))

    def test_three_independent_groups(self):
        """Three independent loop groups create 3D cartesian product."""
        test = PipelineTest()
        test.vars = {
            'a.x': [1, 2],
            'b.y': [10, 20],
            'c.z': [100, 200]
        }
        test.loop = [['a.x'], ['b.y'], ['c.z']]
        test._build_combinations()

        # 2 * 2 * 2 = 8 combinations
        self.assertEqual(len(test.combinations), 8)

    def test_single_value_variable(self):
        """Variable with single value should work correctly."""
        test = PipelineTest()
        test.vars = {
            'a.x': [1, 2, 3],
            'b.y': [100]  # Single value
        }
        test.loop = [['a.x'], ['b.y']]
        test._build_combinations()

        # 3 * 1 = 3 combinations
        self.assertEqual(len(test.combinations), 3)
        for combo in test.combinations:
            self.assertEqual(combo['b.y'], 100)


class TestPipelineTestVariableApplication(unittest.TestCase):
    """Tests for applying variables to pipeline configuration."""

    def test_apply_single_variable(self):
        """Apply single variable to config."""
        test = PipelineTest()
        base_config = {
            'name': 'test',
            'pkgs': [
                {'pkg_type': 'builtin.ior', 'pkg_name': 'ior', 'nprocs': 1}
            ]
        }
        variables = {'ior.nprocs': 8}

        result = test._apply_variables(base_config, variables)

        self.assertEqual(result['pkgs'][0]['nprocs'], 8)

    def test_apply_multiple_variables(self):
        """Apply multiple variables to config."""
        test = PipelineTest()
        base_config = {
            'name': 'test',
            'pkgs': [
                {'pkg_type': 'builtin.ior', 'pkg_name': 'ior', 'nprocs': 1, 'block': '1G'}
            ]
        }
        variables = {'ior.nprocs': 8, 'ior.block': '4G'}

        result = test._apply_variables(base_config, variables)

        self.assertEqual(result['pkgs'][0]['nprocs'], 8)
        self.assertEqual(result['pkgs'][0]['block'], '4G')

    def test_apply_variable_to_multiple_packages(self):
        """Apply variables to different packages."""
        test = PipelineTest()
        base_config = {
            'name': 'test',
            'pkgs': [
                {'pkg_type': 'builtin.app1', 'pkg_name': 'app1', 'param': 'a'},
                {'pkg_type': 'builtin.app2', 'pkg_name': 'app2', 'param': 'b'}
            ]
        }
        variables = {'app1.param': 'x', 'app2.param': 'y'}

        result = test._apply_variables(base_config, variables)

        self.assertEqual(result['pkgs'][0]['param'], 'x')
        self.assertEqual(result['pkgs'][1]['param'], 'y')

    def test_apply_variable_does_not_modify_original(self):
        """Applying variables should not modify the original config."""
        test = PipelineTest()
        base_config = {
            'name': 'test',
            'pkgs': [
                {'pkg_type': 'builtin.ior', 'pkg_name': 'ior', 'nprocs': 1}
            ]
        }
        variables = {'ior.nprocs': 8}

        result = test._apply_variables(base_config, variables)

        # Original should be unchanged
        self.assertEqual(base_config['pkgs'][0]['nprocs'], 1)
        # Result should have new value
        self.assertEqual(result['pkgs'][0]['nprocs'], 8)

    def test_apply_variable_invalid_format_raises_error(self):
        """Variable without dot separator should raise ValueError."""
        test = PipelineTest()
        base_config = {
            'name': 'test',
            'pkgs': []
        }
        variables = {'invalid_no_dot': 5}

        with self.assertRaises(ValueError) as ctx:
            test._apply_variables(base_config, variables)

        self.assertIn('Invalid variable format', str(ctx.exception))

    def test_apply_variable_unknown_package_raises_error(self):
        """Variable referencing unknown package should raise ValueError."""
        test = PipelineTest()
        base_config = {
            'name': 'test',
            'pkgs': [
                {'pkg_type': 'builtin.ior', 'pkg_name': 'ior', 'nprocs': 1}
            ]
        }
        variables = {'unknown_pkg.nprocs': 8}

        with self.assertRaises(ValueError) as ctx:
            test._apply_variables(base_config, variables)

        self.assertIn('Package', str(ctx.exception))
        self.assertIn('not found', str(ctx.exception))

    def test_apply_variable_uses_pkg_type_fallback(self):
        """When pkg_name not specified, should use pkg_type as fallback."""
        test = PipelineTest()
        base_config = {
            'name': 'test',
            'pkgs': [
                {'pkg_type': 'builtin.ior', 'nprocs': 1}  # No pkg_name
            ]
        }
        variables = {'ior.nprocs': 8}  # Uses the pkg_type suffix

        result = test._apply_variables(base_config, variables)

        self.assertEqual(result['pkgs'][0]['nprocs'], 8)


class TestPipelineTestLoading(unittest.TestCase):
    """Tests for loading pipeline test from YAML file."""

    def setUp(self):
        """Create temporary directory for test files."""
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_')

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.test_dir)

    def create_yaml_file(self, content, filename='test.yaml'):
        """Helper to create a YAML file."""
        path = os.path.join(self.test_dir, filename)
        with open(path, 'w') as f:
            yaml.dump(content, f)
        return path

    def test_load_basic_pipeline_test(self):
        """Load a basic pipeline test YAML file."""
        content = {
            'config': {
                'name': 'basic_test',
                'pkgs': [
                    {'pkg_type': 'builtin.example', 'pkg_name': 'example', 'param': 1}
                ]
            },
            'vars': {
                'example.param': [1, 2, 3]
            },
            'loop': [['example.param']],
            'repeat': 2,
            'output': '/tmp/test_output'
        }
        yaml_path = self.create_yaml_file(content)

        test = PipelineTest()
        test.load(yaml_path)

        self.assertEqual(test.name, 'basic_test')
        self.assertEqual(test.repeat, 2)
        self.assertEqual(test.output, '/tmp/test_output')
        self.assertEqual(len(test.combinations), 3)

    def test_load_with_environment_variable_in_output(self):
        """Output path with environment variable should be expanded."""
        os.environ['TEST_OUTPUT_DIR'] = '/custom/output'

        content = {
            'config': {
                'name': 'env_test',
                'pkgs': []
            },
            'output': '${TEST_OUTPUT_DIR}/results'
        }
        yaml_path = self.create_yaml_file(content)

        test = PipelineTest()
        test.load(yaml_path)

        self.assertEqual(test.output, '/custom/output/results')

        # Clean up
        del os.environ['TEST_OUTPUT_DIR']

    def test_load_missing_config_raises_error(self):
        """YAML without config section should raise ValueError."""
        content = {
            'vars': {
                'pkg.param': [1, 2, 3]
            }
        }
        yaml_path = self.create_yaml_file(content)

        test = PipelineTest()
        with self.assertRaises(ValueError) as ctx:
            test.load(yaml_path)

        self.assertIn('config', str(ctx.exception))

    def test_load_nonexistent_file_raises_error(self):
        """Loading nonexistent file should raise FileNotFoundError."""
        test = PipelineTest()
        with self.assertRaises(FileNotFoundError):
            test.load('/nonexistent/path/test.yaml')

    def test_load_defaults_without_optional_fields(self):
        """Optional fields should have sensible defaults."""
        content = {
            'config': {
                'name': 'minimal_test',
                'pkgs': []
            }
        }
        yaml_path = self.create_yaml_file(content)

        test = PipelineTest()
        test.load(yaml_path)

        self.assertEqual(test.vars, {})
        self.assertEqual(test.loop, [])
        self.assertEqual(test.repeat, 1)
        self.assertIsNone(test.output)
        self.assertEqual(len(test.combinations), 1)


class TestLoadYamlAuto(unittest.TestCase):
    """Tests for load_yaml_auto() function."""

    def setUp(self):
        """Create temporary directory for test files."""
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_')

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.test_dir)

    def create_yaml_file(self, content, filename='test.yaml'):
        """Helper to create a YAML file."""
        path = os.path.join(self.test_dir, filename)
        with open(path, 'w') as f:
            yaml.dump(content, f)
        return path

    def test_auto_detect_pipeline_test(self):
        """Should detect and load pipeline test format."""
        content = {
            'config': {
                'name': 'auto_test',
                'pkgs': []
            },
            'vars': {
                'pkg.param': [1, 2]
            }
        }
        yaml_path = self.create_yaml_file(content)

        is_test, obj = load_yaml_auto(yaml_path)

        self.assertTrue(is_test)
        self.assertIsInstance(obj, PipelineTest)
        self.assertEqual(obj.name, 'auto_test')

    def test_nonexistent_file_raises_error(self):
        """Nonexistent file should raise FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            load_yaml_auto('/nonexistent/file.yaml')


class TestPipelineTestResultsOutput(unittest.TestCase):
    """Tests for results output functionality."""

    def setUp(self):
        """Create temporary directory for test files."""
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_')

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.test_dir)

    def test_write_csv_log_creates_csv(self):
        """CSV log should be written to CSV file."""
        output_dir = os.path.join(self.test_dir, 'output')

        test = PipelineTest()
        test.name = 'csv_test'
        test.vars = {'pkg.param': [1, 2]}
        test.loop = [['pkg.param']]
        test.repeat = 1
        test.output = output_dir
        test.results = [
            {
                'combination_idx': 0,
                'repeat_idx': 0,
                'variables': {'pkg.param': 1},
                'status': 'success',
                'runtime': 10.5,
                'stats': {'pkg.throughput': 100}
            },
            {
                'combination_idx': 1,
                'repeat_idx': 0,
                'variables': {'pkg.param': 2},
                'status': 'success',
                'runtime': 11.2,
                'stats': {'pkg.throughput': 110}
            }
        ]

        test._write_csv_log()

        # Check CSV file exists
        csv_path = os.path.join(output_dir, 'results.csv')
        self.assertTrue(os.path.exists(csv_path))

        # Check CSV content
        with open(csv_path, 'r') as f:
            content = f.read()
            self.assertIn('run_idx', content)
            self.assertIn('status', content)
            self.assertIn('pkg.param', content)
            self.assertIn('pkg.throughput', content)

    def test_write_yaml_results_creates_yaml(self):
        """Results should be written to YAML file."""
        output_dir = os.path.join(self.test_dir, 'output')

        test = PipelineTest()
        test.name = 'yaml_test'
        test.vars = {'pkg.param': [1]}
        test.loop = [['pkg.param']]
        test.repeat = 1
        test.output = output_dir
        test.results = [
            {
                'combination_idx': 0,
                'repeat_idx': 0,
                'variables': {'pkg.param': 1},
                'status': 'success'
            }
        ]

        test._write_yaml_results()

        # Check YAML file exists
        yaml_path = os.path.join(output_dir, 'results.yaml')
        self.assertTrue(os.path.exists(yaml_path))

        # Check YAML content
        with open(yaml_path, 'r') as f:
            data = yaml.safe_load(f)
            self.assertEqual(data['test_name'], 'yaml_test')
            self.assertEqual(data['total_runs'], 1)
            self.assertEqual(len(data['results']), 1)

    def test_write_yaml_results_no_output_skips(self):
        """No output directory should skip writing."""
        test = PipelineTest()
        test.name = 'no_output_test'
        test.output = None
        test.results = []

        # Should not raise any errors
        test._write_yaml_results()


class TestPipelineTestResume(unittest.TestCase):
    """Tests for resume functionality via CSV log."""

    def setUp(self):
        """Create temporary directory for test files."""
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_')

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.test_dir)

    def _create_csv(self, output_dir, rows, fieldnames=None):
        """Helper to create a CSV file with given rows."""
        os.makedirs(output_dir, exist_ok=True)
        csv_path = os.path.join(output_dir, 'results.csv')
        if fieldnames is None:
            fieldnames = ['run_idx', 'combination_idx', 'repeat_idx',
                          'status', 'runtime', 'pkg.param', 'error']
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return csv_path

    def test_resume_skips_completed_runs(self):
        """Resume should load existing results and report count."""
        output_dir = os.path.join(self.test_dir, 'output')

        # Create CSV with 2 completed rows
        rows = [
            {'run_idx': 0, 'combination_idx': 0, 'repeat_idx': 0,
             'status': 'success', 'runtime': '10.5', 'pkg.param': '1', 'error': ''},
            {'run_idx': 1, 'combination_idx': 1, 'repeat_idx': 0,
             'status': 'success', 'runtime': '11.2', 'pkg.param': '2', 'error': ''},
        ]
        self._create_csv(output_dir, rows)

        test = PipelineTest()
        test.vars = {'pkg.param': [1, 2, 3]}
        test.loop = [['pkg.param']]
        test.repeat = 1
        test.output = output_dir
        test._build_combinations()

        completed = test._load_csv_log()

        self.assertEqual(completed, 2)
        self.assertEqual(len(test.results), 2)

    def test_resume_empty_csv(self):
        """Resume should return 0 for missing CSV."""
        output_dir = os.path.join(self.test_dir, 'output')

        test = PipelineTest()
        test.vars = {'pkg.param': [1, 2]}
        test.loop = [['pkg.param']]
        test.repeat = 1
        test.output = output_dir
        test._build_combinations()

        completed = test._load_csv_log()

        self.assertEqual(completed, 0)
        self.assertEqual(len(test.results), 0)

    def test_resume_no_output(self):
        """Resume should return 0 when output is None."""
        test = PipelineTest()
        test.output = None

        completed = test._load_csv_log()

        self.assertEqual(completed, 0)

    def test_resume_reconstructs_results(self):
        """Loaded results should have correct structure."""
        output_dir = os.path.join(self.test_dir, 'output')

        fieldnames = ['run_idx', 'combination_idx', 'repeat_idx',
                      'status', 'runtime', 'pkg.param', 'pkg.throughput', 'error']
        rows = [
            {'run_idx': 0, 'combination_idx': 0, 'repeat_idx': 0,
             'status': 'success', 'runtime': '10.5', 'pkg.param': '1',
             'pkg.throughput': '100', 'error': ''},
        ]
        self._create_csv(output_dir, rows, fieldnames)

        test = PipelineTest()
        test.vars = {'pkg.param': [1, 2]}
        test.loop = [['pkg.param']]
        test.repeat = 1
        test.output = output_dir
        test._build_combinations()

        test._load_csv_log()

        result = test.results[0]
        self.assertEqual(result['combination_idx'], 0)
        self.assertEqual(result['repeat_idx'], 0)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['runtime'], 10.5)
        self.assertEqual(result['variables'], {'pkg.param': 1})
        self.assertIn('pkg.throughput', result.get('stats', {}))
        self.assertEqual(result['stats']['pkg.throughput'], 100)

    def test_resume_too_many_rows_starts_fresh(self):
        """CSV with more rows than total runs should start fresh."""
        output_dir = os.path.join(self.test_dir, 'output')

        # Create CSV with 5 rows but test only has 2 total runs
        rows = [
            {'run_idx': i, 'combination_idx': 0, 'repeat_idx': 0,
             'status': 'success', 'runtime': '10.0', 'pkg.param': '1', 'error': ''}
            for i in range(5)
        ]
        self._create_csv(output_dir, rows)

        test = PipelineTest()
        test.vars = {'pkg.param': [1, 2]}
        test.loop = [['pkg.param']]
        test.repeat = 1
        test.output = output_dir
        test._build_combinations()

        completed = test._load_csv_log()

        self.assertEqual(completed, 0)
        self.assertEqual(len(test.results), 0)


class TestPipelineTestCsvLog(unittest.TestCase):
    """Tests for incremental CSV logging."""

    def setUp(self):
        """Create temporary directory for test files."""
        self.test_dir = tempfile.mkdtemp(prefix='jarvis_test_')

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.test_dir)

    def test_csv_log_written_incrementally(self):
        """CSV log should exist after first result is written."""
        output_dir = os.path.join(self.test_dir, 'output')

        test = PipelineTest()
        test.output = output_dir
        test.results = [
            {
                'combination_idx': 0,
                'repeat_idx': 0,
                'variables': {'pkg.param': 1},
                'status': 'success',
                'runtime': 10.5,
                'stats': {}
            }
        ]

        test._write_csv_log()

        csv_path = os.path.join(output_dir, 'results.csv')
        self.assertTrue(os.path.exists(csv_path))

    def test_csv_log_contains_all_results(self):
        """CSV log row count should match result count."""
        output_dir = os.path.join(self.test_dir, 'output')

        test = PipelineTest()
        test.output = output_dir
        test.results = [
            {
                'combination_idx': i,
                'repeat_idx': 0,
                'variables': {'pkg.param': i + 1},
                'status': 'success',
                'runtime': 10.0 + i,
                'stats': {'pkg.throughput': 100 + i * 10}
            }
            for i in range(5)
        ]

        test._write_csv_log()

        csv_path = os.path.join(output_dir, 'results.csv')
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.assertEqual(len(rows), 5)

    def test_csv_log_no_output_skips(self):
        """No output directory should skip CSV writing."""
        test = PipelineTest()
        test.output = None
        test.results = [{'status': 'success'}]

        # Should not raise any errors
        test._write_csv_log()


if __name__ == '__main__':
    unittest.main()
