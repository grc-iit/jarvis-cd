"""
Integration tests for the pipeline test runner.

Tests the full end-to-end flow:
  1. Pipeline iterator runs a mock package with varying parameters
  2. _get_stat extracts custom statistics from stdout
  3. Results are written to CSV
  4. _plot generates plots from the CSV and stores them in the output directory
"""
import csv
import os
import re
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from jarvis_cd.core.pipeline_test import PipelineTest


class MockExec:
    """Simulates Exec.stdout produced by a benchmark run."""

    def __init__(self, stdout_text):
        self.stdout = {'localhost': stdout_text}


class MockBenchPackage:
    """
    A fake benchmark package that:
      - start() produces deterministic stdout with Bandwidth and Latency
      - _get_stat() parses those metrics
      - _plot() generates a simple plot image
    """

    def __init__(self, pkg_id, config):
        self.pkg_id = pkg_id
        self.config = config
        self.env = {}
        self.mod_env = {}
        self.exec = None

    def start(self):
        warps = self.config.get('warps', 1)
        io_size = self.config.get('io_size', '128k')
        # Produce fake benchmark output with deterministic values
        bw = 10.0 + warps * 0.5
        lat = 100.0 - warps * 2.0
        self.exec = MockExec(
            f"=== Mock GPU Benchmark ===\n"
            f"Warps: {warps}\n"
            f"IO size: {io_size}\n"
            f"Bandwidth: {bw:.2f} GB/s\n"
            f"Latency: {lat:.2f} us\n"
            f"Done.\n"
        )

    def stop(self):
        pass

    def _get_stat(self, stat_dict):
        output = self.exec.stdout['localhost']
        bw = re.search(r'Bandwidth:\s+([0-9.]+)\s+GB/s', output)
        if bw:
            stat_dict[f'{self.pkg_id}.bandwidth_gbps'] = float(bw.group(1))
        lat = re.search(r'Latency:\s+([0-9.]+)\s+us', output)
        if lat:
            stat_dict[f'{self.pkg_id}.latency_us'] = float(lat.group(1))
        stat_dict[f'{self.pkg_id}.warps'] = self.config.get('warps', 1)
        stat_dict[f'{self.pkg_id}.io_size'] = self.config.get('io_size', '128k')

    def _plot(self, results_csv, output_dir):
        """Generate simple plot files from CSV results."""
        try:
            import pandas as pd
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
        except ImportError:
            # Write a marker file so the test can still verify _plot was called
            with open(os.path.join(output_dir, 'plot_called.txt'), 'w') as f:
                f.write('_plot was called but pandas/matplotlib not available')
            return

        df = pd.read_csv(results_csv)

        bw_col = None
        warps_col = None
        io_col = None
        for col in df.columns:
            if col.endswith('.bandwidth_gbps'):
                bw_col = col
            elif col.endswith('.warps'):
                warps_col = col
            elif col.endswith('.io_size'):
                io_col = col

        if not bw_col:
            return

        # Bandwidth vs warps
        if warps_col and len(df[warps_col].dropna().unique()) > 1:
            fig, ax = plt.subplots(figsize=(6, 4))
            grouped = df.groupby(warps_col)[bw_col].mean()
            grouped.plot(kind='bar', ax=ax)
            ax.set_xlabel('Warps')
            ax.set_ylabel('Bandwidth (GB/s)')
            ax.set_title('Mock Bandwidth vs Warp Count')
            fig.tight_layout()
            fig.savefig(os.path.join(output_dir, 'bandwidth_vs_warps.png'),
                        dpi=72)
            plt.close(fig)

        # Bandwidth vs IO size
        if io_col and len(df[io_col].dropna().unique()) > 1:
            fig, ax = plt.subplots(figsize=(6, 4))
            grouped = df.groupby(io_col)[bw_col].mean()
            grouped.plot(kind='bar', ax=ax)
            ax.set_xlabel('I/O Size')
            ax.set_ylabel('Bandwidth (GB/s)')
            ax.set_title('Mock Bandwidth vs I/O Size')
            fig.tight_layout()
            fig.savefig(os.path.join(output_dir, 'bandwidth_vs_iosize.png'),
                        dpi=72)
            plt.close(fig)


class MockPipeline:
    """Minimal pipeline stand-in that tracks started instances."""

    def __init__(self, config):
        self.name = config.get('name', 'mock_pipeline')
        self.packages = []
        self.env = {}
        self._started_instances = []

        for pkg_def in config.get('pkgs', []):
            pkg_name = pkg_def.get('pkg_name',
                                   pkg_def.get('pkg_type', '').split('.')[-1])
            entry = {
                'pkg_type': pkg_def['pkg_type'],
                'pkg_name': pkg_name,
                'pkg_id': pkg_name,
                'global_id': f'{self.name}.{pkg_name}',
                'config': {k: v for k, v in pkg_def.items()
                           if k not in ('pkg_type', 'pkg_name')},
            }
            self.packages.append(entry)

    def load(self, fmt, path):
        pass

    def build_container_if_needed(self):
        pass

    def configure_all_packages(self):
        pass

    def is_containerized(self):
        return False

    def start(self):
        self._started_instances = []
        for pkg_def in self.packages:
            inst = MockBenchPackage(pkg_def['pkg_id'], pkg_def['config'])
            inst.start()
            self._started_instances.append(inst)

    def stop(self):
        for inst in self._started_instances:
            inst.stop()


def _make_mock_pipeline(config):
    """Factory that returns a MockPipeline pre-loaded with the given config."""
    return MockPipeline(config)


class TestPipelineTestIntegration(unittest.TestCase):
    """
    End-to-end integration test:
      YAML test config  ->  pipeline iterator  ->  _get_stat  ->  CSV  ->  _plot  ->  PNG
    """

    def setUp(self):
        self.output_dir = tempfile.mkdtemp(prefix='jarvis_integ_test_')

    def tearDown(self):
        shutil.rmtree(self.output_dir)

    def _run_pipeline_test(self, test_obj):
        """
        Run a PipelineTest with MockPipeline injected in place of the real
        Pipeline class.
        """
        import jarvis_cd.core.pipeline_test as pt_mod

        original_run_single = test_obj._run_single

        def patched_run_single(config, variables, repeat_idx):
            """Replace _run_single to use MockPipeline instead of real Pipeline."""
            import time
            from datetime import datetime

            result = {
                'combination_idx': (
                    test_obj.combinations.index(variables)
                    if variables in test_obj.combinations else 0
                ),
                'repeat_idx': repeat_idx,
                'variables': variables.copy(),
                'start_time': datetime.now().isoformat(),
            }

            pipeline = _make_mock_pipeline(config)
            start_time = time.time()
            pipeline.start()
            pipeline.stop()
            end_time = time.time()
            result['runtime'] = end_time - start_time

            # Collect stats from the started instances (the bug fix path)
            stat_dict = {}
            for inst in pipeline._started_instances:
                if hasattr(inst, '_get_stat'):
                    inst._get_stat(stat_dict)
            result['stats'] = stat_dict

            # Store for _run_plots
            test_obj._last_pipeline = pipeline

            result['end_time'] = datetime.now().isoformat()
            return result

        test_obj._run_single = patched_run_single

        # Also patch _run_plots to use the stored pipeline's instances
        original_run_plots = test_obj._run_plots

        def patched_run_plots(pipeline=None):
            if not test_obj.output:
                return
            csv_path = os.path.join(test_obj.output, 'results.csv')
            if not os.path.exists(csv_path):
                return
            last_pipeline = getattr(test_obj, '_last_pipeline', None)
            instances = (getattr(last_pipeline, '_started_instances', [])
                         if last_pipeline else [])
            if instances:
                for inst in instances:
                    if hasattr(inst, '_plot'):
                        inst._plot(csv_path, test_obj.output)
            elif pipeline:
                original_run_plots(pipeline)

        test_obj._run_plots = patched_run_plots
        test_obj.run()

    def test_full_grid_search_with_stats_and_plots(self):
        """
        Run a 2D grid search (warps x io_size), verify:
          - CSV has correct stats columns
          - Bandwidth values are deterministic and correct
          - Plot PNGs are generated in output dir
        """
        test = PipelineTest()
        test.name = 'mock_bench_test'
        test.config = {
            'name': 'mock_bench_test',
            'pkgs': [{
                'pkg_type': 'mock.bench',
                'pkg_name': 'bench',
                'warps': 1,
                'io_size': '128k',
            }],
        }
        test.vars = {
            'bench.warps': [1, 2, 4, 8],
            'bench.io_size': ['64k', '256k', '1m'],
        }
        test.loop = [['bench.warps'], ['bench.io_size']]
        test.repeat = 1
        test.output = self.output_dir
        test._build_combinations()

        self._run_pipeline_test(test)

        # --- Verify CSV ---
        csv_path = os.path.join(self.output_dir, 'results.csv')
        self.assertTrue(os.path.exists(csv_path),
                        "results.csv was not created")

        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        # 4 warps x 3 io_sizes x 1 repeat = 12 rows
        self.assertEqual(len(rows), 12,
                         f"Expected 12 rows, got {len(rows)}")

        # Check that custom stat columns exist
        self.assertIn('bench.bandwidth_gbps', reader.fieldnames)
        self.assertIn('bench.latency_us', reader.fieldnames)
        self.assertIn('bench.warps', reader.fieldnames)
        self.assertIn('bench.io_size', reader.fieldnames)

        # Verify deterministic bandwidth for warps=4
        for row in rows:
            if row.get('bench.warps') == '4':
                bw = float(row['bench.bandwidth_gbps'])
                # bw = 10.0 + warps * 0.5 = 12.0
                self.assertAlmostEqual(bw, 12.0, places=1,
                                       msg=f"Expected bw=12.0 for warps=4, got {bw}")

        # All rows should be successful
        for row in rows:
            self.assertEqual(row['status'], 'success')

        # --- Verify plots ---
        try:
            import pandas  # noqa: F401
            import matplotlib  # noqa: F401
            # If matplotlib is available, PNGs should exist
            warps_plot = os.path.join(self.output_dir,
                                      'bandwidth_vs_warps.png')
            iosize_plot = os.path.join(self.output_dir,
                                       'bandwidth_vs_iosize.png')
            self.assertTrue(os.path.exists(warps_plot),
                            "bandwidth_vs_warps.png not generated")
            self.assertTrue(os.path.exists(iosize_plot),
                            "bandwidth_vs_iosize.png not generated")
            # Plots should be non-trivial files
            self.assertGreater(os.path.getsize(warps_plot), 1000,
                               "warps plot is suspiciously small")
            self.assertGreater(os.path.getsize(iosize_plot), 1000,
                               "iosize plot is suspiciously small")
        except ImportError:
            # Without matplotlib, verify _plot at least ran
            marker = os.path.join(self.output_dir, 'plot_called.txt')
            self.assertTrue(os.path.exists(marker),
                            "_plot was not called at all")

    def test_zipped_variables_with_repeat(self):
        """
        Zipped variables with repeat > 1 — verifies that stats are collected
        for every repeat and that CSV contains the right number of rows.
        """
        test = PipelineTest()
        test.name = 'zipped_test'
        test.config = {
            'name': 'zipped_test',
            'pkgs': [{
                'pkg_type': 'mock.bench',
                'pkg_name': 'bench',
                'warps': 1,
                'io_size': '128k',
            }],
        }
        # Zipped: (warps, io_size) pairs
        test.vars = {
            'bench.warps': [1, 4, 16],
            'bench.io_size': ['64k', '256k', '1m'],
        }
        test.loop = [['bench.warps', 'bench.io_size']]
        test.repeat = 2
        test.output = self.output_dir
        test._build_combinations()

        self._run_pipeline_test(test)

        csv_path = os.path.join(self.output_dir, 'results.csv')
        with open(csv_path, 'r') as f:
            rows = list(csv.DictReader(f))

        # 3 zipped combos x 2 repeats = 6 rows
        self.assertEqual(len(rows), 6)

        # Check that bandwidth is non-empty for every row
        for row in rows:
            self.assertTrue(row['bench.bandwidth_gbps'],
                            "bandwidth_gbps should not be empty")
            bw = float(row['bench.bandwidth_gbps'])
            self.assertGreater(bw, 0)

    def test_single_run_no_vars(self):
        """
        Single run with no vars — baseline case.
        """
        test = PipelineTest()
        test.name = 'single_test'
        test.config = {
            'name': 'single_test',
            'pkgs': [{
                'pkg_type': 'mock.bench',
                'pkg_name': 'bench',
                'warps': 8,
                'io_size': '1m',
            }],
        }
        test.vars = {}
        test.loop = []
        test.repeat = 1
        test.output = self.output_dir
        test._build_combinations()

        self._run_pipeline_test(test)

        csv_path = os.path.join(self.output_dir, 'results.csv')
        with open(csv_path, 'r') as f:
            rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        bw = float(rows[0]['bench.bandwidth_gbps'])
        # bw = 10.0 + 8 * 0.5 = 14.0
        self.assertAlmostEqual(bw, 14.0, places=1)

    def test_yaml_results_written(self):
        """Verify results.yaml is also written alongside CSV."""
        import yaml

        test = PipelineTest()
        test.name = 'yaml_check'
        test.config = {
            'name': 'yaml_check',
            'pkgs': [{
                'pkg_type': 'mock.bench',
                'pkg_name': 'bench',
                'warps': 2,
                'io_size': '64k',
            }],
        }
        test.vars = {'bench.warps': [1, 2]}
        test.loop = [['bench.warps']]
        test.repeat = 1
        test.output = self.output_dir
        test._build_combinations()

        self._run_pipeline_test(test)

        yaml_path = os.path.join(self.output_dir, 'results.yaml')
        self.assertTrue(os.path.exists(yaml_path))

        with open(yaml_path, 'r') as f:
            data = yaml.safe_load(f)

        self.assertEqual(data['test_name'], 'yaml_check')
        self.assertEqual(data['total_runs'], 2)
        self.assertEqual(len(data['results']), 2)

        # Stats should be present in YAML results
        for result in data['results']:
            self.assertIn('stats', result)
            self.assertIn('bench.bandwidth_gbps', result['stats'])


if __name__ == '__main__':
    unittest.main()
