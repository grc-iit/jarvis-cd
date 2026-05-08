"""
Pipeline test management for Jarvis-CD.
Provides the PipelineTest class for running experiment sets using grid search.
"""

import os
import csv
import yaml
import copy
import itertools
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from jarvis_cd.core.pipeline import Pipeline
from jarvis_cd.util.logger import logger, Color


def is_pipeline_test(yaml_data: Dict[str, Any]) -> bool:
    """
    Detect if a YAML configuration is a pipeline test script.

    A pipeline test has a 'config' section containing the pipeline definition,
    and at least one of: 'vars', 'loop', 'repeat', or 'output'.

    A regular pipeline has the pipeline definition at the top level
    (name, pkgs, etc.).

    :param yaml_data: Parsed YAML data
    :return: True if this is a pipeline test, False if regular pipeline
    """
    # Check for pipeline test structure
    has_config = 'config' in yaml_data
    has_test_fields = any(key in yaml_data for key in ['vars', 'loop', 'repeat', 'output'])

    # A pipeline test must have 'config' section
    if has_config and has_test_fields:
        return True

    # A regular pipeline has top-level 'name' or 'pkgs'
    if 'name' in yaml_data or 'pkgs' in yaml_data:
        return False

    # Default to test if 'config' is present
    return has_config


class PipelineTest:
    """
    Pipeline test runner for experiment sets using grid search.

    Handles:
    - Parsing test YAML configuration
    - Building grid search combinations based on vars and loop
    - Running each configuration repeat times
    - Collecting statistics and writing output
    """

    def __init__(self):
        """Initialize pipeline test instance."""
        self.name = None
        self.config = {}  # The base pipeline configuration
        self.vars = {}  # Variable definitions
        self.loop = []  # Loop structure
        self.repeat = 1
        self.output = None
        self.combinations = []  # Generated test combinations
        self.results = []  # Collected results

    @staticmethod
    def _parse_csv_value(value_str):
        """
        Parse a CSV string value back to a Python type.

        Attempts int, then float, then falls back to string.

        :param value_str: String value from CSV
        :return: Parsed Python value
        """
        try:
            return int(value_str)
        except (ValueError, TypeError):
            pass
        try:
            return float(value_str)
        except (ValueError, TypeError):
            pass
        return value_str

    def _load_csv_log(self):
        """
        Load previously completed results from CSV log for resume support.

        Reads {output}/results.csv and reconstructs result dicts.

        :return: Number of loaded results (0 if no CSV or output is None)
        """
        if not self.output:
            return 0

        csv_path = Path(self.output) / 'results.csv'
        if not csv_path.exists():
            return 0

        # Determine known variable names
        all_var_names = set()
        for combo in self.combinations:
            all_var_names.update(combo.keys())

        loaded_results = []
        try:
            with open(csv_path, 'r', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    result = {
                        'combination_idx': int(row.get('combination_idx', 0)),
                        'repeat_idx': int(row.get('repeat_idx', 0)),
                        'status': row.get('status', 'unknown'),
                    }

                    # Parse runtime
                    runtime_str = row.get('runtime', '')
                    if runtime_str:
                        result['runtime'] = self._parse_csv_value(runtime_str)

                    # Parse variables (known var names)
                    variables = {}
                    for var_name in all_var_names:
                        if var_name in row and row[var_name] != '':
                            variables[var_name] = self._parse_csv_value(row[var_name])
                    result['variables'] = variables

                    # Parse stats (remaining columns)
                    meta_keys = {'run_idx', 'combination_idx', 'repeat_idx',
                                 'status', 'runtime', 'error'}
                    stat_keys = set(row.keys()) - meta_keys - all_var_names
                    stats = {}
                    for key in stat_keys:
                        if row[key] != '':
                            stats[key] = self._parse_csv_value(row[key])
                    if stats:
                        result['stats'] = stats

                    # Parse error
                    error_str = row.get('error', '')
                    if error_str:
                        result['error'] = error_str

                    loaded_results.append(result)
        except Exception as e:
            logger.warning(f"Failed to load CSV log: {e}")
            return 0

        # Sanity check
        total_runs = len(self.combinations) * self.repeat
        if len(loaded_results) > total_runs:
            logger.warning(
                f"CSV log has {len(loaded_results)} results but test only has "
                f"{total_runs} total runs. Starting fresh."
            )
            return 0

        self.results = loaded_results
        return len(loaded_results)

    def _write_csv_log(self):
        """
        Write all current results to CSV log file.

        Full overwrite each time to handle dynamic stat columns correctly.
        Called after each run completes.
        """
        if not self.output:
            return

        output_dir = Path(self.output)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Get all variable names and stat keys
        all_var_names = set()
        all_stat_keys = set()
        for result in self.results:
            all_var_names.update(result.get('variables', {}).keys())
            all_stat_keys.update(result.get('stats', {}).keys())

        all_var_names = sorted(all_var_names)
        all_stat_keys = sorted(all_stat_keys)

        csv_path = output_dir / 'results.csv'
        with open(csv_path, 'w', newline='') as f:
            fieldnames = ['run_idx', 'combination_idx', 'repeat_idx', 'status', 'runtime'] + \
                        all_var_names + all_stat_keys + ['error']
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()

            for idx, result in enumerate(self.results):
                row = {
                    'run_idx': idx,
                    'combination_idx': result.get('combination_idx', 0),
                    'repeat_idx': result.get('repeat_idx', 0),
                    'status': result.get('status', 'unknown'),
                    'runtime': result.get('runtime', ''),
                    'error': result.get('error', '')
                }
                # Add variables
                row.update(result.get('variables', {}))
                # Add stats
                row.update(result.get('stats', {}))

                writer.writerow(row)

    def _print_params(self, combo):
        """Print parameter values for a combination."""
        if combo:
            params_str = ', '.join(f'{k}={v}' for k, v in combo.items())
            logger.print(Color.CYAN, f"  Parameters: {params_str}")
        else:
            logger.print(Color.CYAN, f"  Parameters: (none)")

    def load(self, pipeline_file: str):
        """
        Load pipeline test from YAML file.

        :param pipeline_file: Path to pipeline test YAML file
        """
        pipeline_file = Path(pipeline_file)
        if not pipeline_file.exists():
            raise FileNotFoundError(f"Pipeline test file not found: {pipeline_file}")

        with open(pipeline_file, 'r') as f:
            test_def = yaml.safe_load(f)

        # Validate structure
        if 'config' not in test_def:
            raise ValueError("Pipeline test must have a 'config' section containing the pipeline definition")

        # Load test components
        self.config = test_def['config']
        self.name = self.config.get('name', pipeline_file.stem)
        self.vars = test_def.get('vars', {})
        self.loop = test_def.get('loop', [])
        self.repeat = test_def.get('repeat', 1)
        self.output = test_def.get('output', None)

        # Expand output path with environment variables
        if self.output:
            self.output = os.path.expandvars(self.output)

        # Build test combinations from vars and loop
        self._build_combinations()

        logger.pipeline(f"Loaded pipeline test: {self.name}")
        logger.info(f"  Variables: {len(self.vars)}")
        logger.info(f"  Loop groups: {len(self.loop)}")
        logger.info(f"  Total combinations: {len(self.combinations)}")
        logger.info(f"  Repeat count: {self.repeat}")
        logger.info(f"  Total runs: {len(self.combinations) * self.repeat}")

    def _build_combinations(self):
        """
        Build test combinations from vars and loop definitions.

        The loop section defines how variables are iterated:
        - Variables in the same list vary together (zip)
        - Variables in different lists are independent (cartesian product)

        Example:
            vars:
              a.x: [1, 2, 3]
              a.y: [10, 20, 30]
              b.z: [100, 200]
            loop:
              - [a.x, a.y]
              - [b.z]

        This produces 6 combinations:
            (1, 10, 100), (1, 10, 200), (2, 20, 100), (2, 20, 200), (3, 30, 100), (3, 30, 200)
        """
        if not self.vars:
            # No variables defined - single configuration
            self.combinations = [{}]
            return

        if not self.loop:
            # No loop defined - create independent loop for each variable
            self.loop = [[var_name] for var_name in self.vars.keys()]

        # Build zipped groups
        loop_groups = []
        for group in self.loop:
            if not group:
                continue

            # Get values for each variable in the group
            group_vars = []
            var_lengths = []
            for var_name in group:
                if var_name not in self.vars:
                    raise ValueError(f"Variable '{var_name}' in loop not defined in vars section")
                values = self.vars[var_name]
                if not isinstance(values, list):
                    values = [values]
                group_vars.append((var_name, values))
                var_lengths.append(len(values))

            # Verify all variables in group have same length
            if len(set(var_lengths)) > 1:
                var_names = [v[0] for v in group_vars]
                raise ValueError(
                    f"Variables in loop group {var_names} must have the same number of values. "
                    f"Found lengths: {dict(zip(var_names, var_lengths))}"
                )

            # Zip the values together
            if group_vars:
                zipped = []
                for i in range(var_lengths[0]):
                    combo = {var_name: values[i] for var_name, values in group_vars}
                    zipped.append(combo)
                loop_groups.append(zipped)

        # Cartesian product of all groups
        if not loop_groups:
            self.combinations = [{}]
        elif len(loop_groups) == 1:
            self.combinations = loop_groups[0]
        else:
            # Cartesian product of groups
            self.combinations = []
            for product in itertools.product(*loop_groups):
                combo = {}
                for group_combo in product:
                    combo.update(group_combo)
                self.combinations.append(combo)

    def _apply_variables(self, base_config: Dict[str, Any], variables: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply variable values to a pipeline configuration.

        Variable names are in the format: pkg_name.var_name

        :param base_config: Base pipeline configuration
        :param variables: Variable values to apply
        :return: Modified configuration
        """
        config = copy.deepcopy(base_config)

        for var_spec, value in variables.items():
            parts = var_spec.split('.', 1)
            if len(parts) != 2:
                raise ValueError(f"Invalid variable format '{var_spec}'. Expected: pkg_name.var_name")

            pkg_name, var_name = parts

            # Find the package in pkgs list
            pkgs = config.get('pkgs', [])
            found = False
            for pkg in pkgs:
                if pkg.get('pkg_name', pkg.get('pkg_type', '').split('.')[-1]) == pkg_name:
                    pkg[var_name] = value
                    found = True
                    break

            if not found:
                raise ValueError(f"Package '{pkg_name}' not found in pipeline configuration")

        return config

    def run(self):
        """
        Run all test combinations.

        Supports resuming from a previous partial run by reading existing CSV
        results and skipping already-completed runs.

        For each combination:
        1. Apply variables to create a modified pipeline configuration
        2. Run the pipeline repeat times
        3. Collect statistics from packages
        4. Store results and write CSV incrementally
        """
        total_runs = len(self.combinations) * self.repeat

        # Try to resume from existing CSV log
        completed_runs = self._load_csv_log()

        if completed_runs > 0:
            logger.pipeline(f"Resuming pipeline test: {self.name}")
            logger.info(f"  Found {completed_runs}/{total_runs} completed runs, "
                        f"resuming from run {completed_runs + 1}")
        else:
            logger.pipeline(f"Starting pipeline test: {self.name}")
            logger.info(f"Total runs: {total_runs}")

        current_run = 0

        for combo_idx, combo in enumerate(self.combinations):
            for repeat_idx in range(self.repeat):
                current_run += 1

                # Skip already-completed runs
                if current_run <= completed_runs:
                    continue

                remaining = total_runs - current_run
                logger.print(
                    Color.CYAN,
                    f"--- BEGIN Iteration {current_run}/{total_runs} "
                    f"({remaining} remaining) ---"
                )
                self._print_params(combo)
                if self.repeat > 1:
                    logger.print(Color.CYAN, f"  Repeat: {repeat_idx + 1}/{self.repeat}")
                if self.output:
                    logger.print(Color.CYAN, f"  Output: {self.output}")

                # Apply variables to configuration
                modified_config = self._apply_variables(self.config, combo)

                # Create a unique name for this run
                run_name = f"{self.name}_run{current_run}"
                modified_config['name'] = run_name

                try:
                    # Run the pipeline
                    result = self._run_single(modified_config, combo, repeat_idx)
                    result['status'] = 'success'
                except Exception as e:
                    logger.error(f"Run failed: {e}")
                    result = {
                        'combination_idx': combo_idx,
                        'repeat_idx': repeat_idx,
                        'variables': combo.copy(),
                        'status': 'failed',
                        'error': str(e)
                    }

                # Log result
                runtime_str = f"{result.get('runtime', 0):.2f}s" if 'runtime' in result else 'N/A'
                logger.info(f"  Status: {result.get('status', 'unknown')}, Runtime: {runtime_str}")

                self.results.append(result)
                self._write_csv_log()

                # Confirm persistence
                if self.output:
                    csv_path = Path(self.output) / 'results.csv'
                    logger.print(Color.CYAN, f"  Results persisted to: {csv_path}")

                logger.print(
                    Color.CYAN,
                    f"--- END Iteration {current_run}/{total_runs} ---"
                )

        # Write final YAML results
        self._write_yaml_results()

        # Generate plots if any package defines _plot
        self._run_plots(getattr(self, '_last_pipeline', None))

        logger.success(f"Pipeline test completed: {len(self.results)} runs")

        # Print summary
        successful = sum(1 for r in self.results if r.get('status') == 'success')
        failed = len(self.results) - successful
        logger.info(f"Summary: {successful} successful, {failed} failed")

    def _run_plots(self, pipeline=None):
        """
        Call _plot on packages that define it.

        Uses the pipeline from the last run (if provided) or creates one from
        the base config to access package instances.  Calls
        _plot(results_csv, output_dir) on each package that has the method.

        :param pipeline: Optional Pipeline instance from the last run
        """
        if not self.output:
            return

        csv_path = str(Path(self.output) / 'results.csv')
        if not os.path.exists(csv_path):
            return

        # If we have started instances from the last run, prefer those
        instances = getattr(pipeline, '_started_instances', []) if pipeline else []

        if instances:
            for pkg_instance in instances:
                try:
                    if hasattr(pkg_instance, '_plot'):
                        logger.info(f"Generating plots for {pkg_instance.pkg_id}")
                        pkg_instance._plot(csv_path, self.output)
                except Exception as e:
                    logger.warning(f"Could not plot from {pkg_instance.pkg_id}: {e}")
        else:
            # Fallback: create a pipeline from base config to get package instances
            try:
                import tempfile
                with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                    yaml.dump(self.config, f, default_flow_style=False)
                    temp_yaml = f.name

                fallback_pipeline = Pipeline()
                fallback_pipeline.load('yaml', temp_yaml)
                os.unlink(temp_yaml)

                for pkg_def in fallback_pipeline.packages:
                    try:
                        pkg_instance = fallback_pipeline._load_package_instance(
                            pkg_def, fallback_pipeline.env)
                        if hasattr(pkg_instance, '_plot'):
                            logger.info(f"Generating plots for {pkg_def.get('pkg_id', 'unknown')}")
                            pkg_instance._plot(csv_path, self.output)
                    except Exception as e:
                        logger.warning(f"Could not plot from {pkg_def.get('pkg_id', 'unknown')}: {e}")

            except Exception as e:
                logger.warning(f"Plot generation failed: {e}")

    def _run_single(self, config: Dict[str, Any], variables: Dict[str, Any], repeat_idx: int) -> Dict[str, Any]:
        """
        Run a single pipeline configuration.

        :param config: Modified pipeline configuration
        :param variables: Variable values used in this run
        :param repeat_idx: Repeat index
        :return: Result dictionary with statistics
        """
        import tempfile
        import time

        # Create result dictionary
        result = {
            'combination_idx': self.combinations.index(variables) if variables in self.combinations else 0,
            'repeat_idx': repeat_idx,
            'variables': variables.copy(),
            'start_time': datetime.now().isoformat()
        }

        # Create temporary YAML file for this configuration
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config, f, default_flow_style=False)
            temp_yaml = f.name

        try:
            # Create and run pipeline
            pipeline = Pipeline()

            start_time = time.time()
            pipeline.load('yaml', temp_yaml)
            pipeline.configure_all_packages()

            # Run the pipeline
            pipeline.start()
            pipeline.stop()

            end_time = time.time()
            result['runtime'] = end_time - start_time

            # Collect statistics from started package instances
            # (must use the same instances that ran start() so they have exec output)
            stat_dict = {}
            for pkg_instance in getattr(pipeline, '_started_instances', []):
                try:
                    if hasattr(pkg_instance, '_get_stat'):
                        pkg_instance._get_stat(stat_dict)
                except Exception as e:
                    logger.warning(f"Could not get stats from {pkg_instance.pkg_id}: {e}")

            result['stats'] = stat_dict

            # Store last pipeline for _run_plots
            self._last_pipeline = pipeline

        finally:
            # Clean up temporary file
            os.unlink(temp_yaml)

        result['end_time'] = datetime.now().isoformat()

        return result

    def _write_yaml_results(self):
        """
        Write results to YAML file.

        Creates results.yaml with full result details.
        CSV output is handled incrementally by _write_csv_log().
        """
        if not self.output:
            logger.info("No output directory specified, skipping result export")
            return

        output_dir = Path(self.output)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Write YAML file with full details
        yaml_path = output_dir / 'results.yaml'
        with open(yaml_path, 'w') as f:
            yaml.dump({
                'test_name': self.name,
                'total_runs': len(self.results),
                'variables': self.vars,
                'loop': self.loop,
                'repeat': self.repeat,
                'results': self.results
            }, f, default_flow_style=False)

        logger.info(f"Full results written to: {yaml_path}")


def load_yaml_auto(pipeline_file: str) -> Tuple[bool, Any]:
    """
    Load a YAML file and determine if it's a pipeline test or regular pipeline.

    :param pipeline_file: Path to YAML file
    :return: Tuple of (is_test, loaded_object)
             - If test: (True, PipelineTest instance)
             - If pipeline: (False, Pipeline instance)
    """
    pipeline_file = Path(pipeline_file)
    if not pipeline_file.exists():
        raise FileNotFoundError(f"Pipeline file not found: {pipeline_file}")

    # Load YAML to detect type
    with open(pipeline_file, 'r') as f:
        yaml_data = yaml.safe_load(f)

    if is_pipeline_test(yaml_data):
        # Load as pipeline test
        test = PipelineTest()
        test.load(str(pipeline_file))
        return True, test
    else:
        # Load as regular pipeline
        pipeline = Pipeline()
        pipeline.load('yaml', str(pipeline_file))
        return False, pipeline


def run_yaml_auto(pipeline_file: str):
    """
    Load and run a YAML file, automatically detecting if it's a test or pipeline.

    :param pipeline_file: Path to YAML file
    """
    is_test, obj = load_yaml_auto(pipeline_file)

    if is_test:
        obj.run()
    else:
        obj.run()
