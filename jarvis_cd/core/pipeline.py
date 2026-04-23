"""
Pipeline management for Jarvis-CD.
Provides the consolidated Pipeline class that combines pipeline creation, loading, and execution.
"""

import os
import socket
import yaml
import copy
from pathlib import Path
from typing import Dict, Any, List, Optional
from jarvis_cd.core.config import load_class, Jarvis
from jarvis_cd.util.logger import logger
from jarvis_cd.util.hostfile import Hostfile


class Pipeline:
    """
    Consolidated pipeline management class.
    Handles pipeline creation, loading, running, and lifecycle management.
    """
    
    def __init__(self, name: str = None):
        """
        Initialize pipeline instance.

        :param name: Pipeline name (optional for new pipelines)
        """
        self.jarvis = Jarvis.get_instance()
        self.name = name
        self.packages = []
        self.interceptors = {}  # Store pipeline-level interceptors by name
        self.env = {}
        self.created_at = None
        self.last_loaded_file = None

        # Container parameters
        self.container_image = ""  # Pre-built image to use
        self.container_engine = "podman"  # Default container engine
        self.container_base = "iowarp/iowarp-build:latest"  # Base image
        self.container_ssh_port = 2222  # Default SSH port for containers
        self.container_extensions = {}  # Custom extensions to Docker compose file
        self.container_env = {}  # Environment variables injected into containers
        self.container_host_path = ""  # Docker host path prefix for DinD remapping
        self.container_workspace = ""  # Container workspace root for DinD remapping

        # Install manager: None (legacy default), 'container', or 'spack'
        self.install_manager = None

        # Hostfile parameter (None means use global jarvis hostfile)
        self.hostfile = None

        # Load existing pipeline if name is provided
        if name:
            self.load()

    def get_hostfile(self) -> Hostfile:
        """
        Get the effective hostfile for this pipeline.
        Falls back to global jarvis hostfile if pipeline hostfile is not set.

        :return: Hostfile object
        """
        if self.hostfile:
            return self.hostfile
        return self.jarvis.hostfile

    def _hostfile_is_local_only(self, hf) -> bool:
        """True when every host in the hostfile is this machine (or hostfile
        is empty). Used to pick LocalExecInfo over PsshExecInfo for
        single-node/local runs without requiring sshd on :22. Real cluster
        hostfiles contain remote hostnames and return False."""
        if not hf or len(hf) == 0:
            return True
        local_names = {'localhost', '127.0.0.1', socket.gethostname()}
        return all(h in local_names for h in hf.hosts)

    def is_containerized(self) -> bool:
        """
        Check if this pipeline uses containers.

        :return: True if pipeline uses a container image
        """
        return bool(self.container_image)

    def _has_containerized_packages(self) -> bool:
        """
        Check if any package in the pipeline has deploy_mode: container.

        :return: True if at least one package uses container deployment
        """
        for pkg_def in self.packages:
            if pkg_def.get('config', {}).get('deploy_mode') == 'container':
                return True
        return False

    def _propagate_deploy_mode(self):
        """
        Derive deploy_mode from install_manager and set it on all packages
        and interceptors.  deploy_mode is pipeline-level, not per-package.
        """
        if self.install_manager == 'container':
            deploy_mode = 'container'
        elif self.install_manager == 'spack':
            deploy_mode = 'default'
        else:
            deploy_mode = 'default'

        for pkg_def in self.packages:
            pkg_def.setdefault('config', {})['deploy_mode'] = deploy_mode
        for idef in self.interceptors.values():
            idef.setdefault('config', {})['deploy_mode'] = deploy_mode

    def _install_spack_packages(self):
        """
        Install packages via spack and capture the resulting environment.

        1. Collect 'install' specs from all packages
        2. Run 'spack install <specs>'
        3. Run 'spack load <specs>' in a subprocess and capture env
        4. Merge spack environment into pipeline environment
        """
        from jarvis_cd.shell import Exec, LocalExecInfo
        from jarvis_cd.core.environment import EnvironmentManager

        # Collect spack specs from all packages
        spack_specs = []
        for pkg_def in self.packages:
            install_spec = pkg_def.get('config', {}).get('install', '')
            if install_spec:
                spack_specs.append(install_spec)

        if not spack_specs:
            print("No spack install specs found in packages, skipping spack install")
            return

        specs_str = ' '.join(spack_specs)

        # Build spack command prefix (source setup-env.sh if SPACK_ROOT is set)
        import os
        spack_root = os.environ.get('SPACK_ROOT', '')
        if spack_root:
            spack_prefix = f'. {spack_root}/share/spack/setup-env.sh && '
        else:
            spack_prefix = ''

        # Step 1: spack install
        print(f"Installing spack packages: {specs_str}")
        install_cmd = f'bash -c "{spack_prefix}spack install {specs_str}"'
        result = Exec(install_cmd, LocalExecInfo()).run()
        exit_code = result.exit_code.get('localhost', 1)
        if exit_code != 0:
            raise RuntimeError(
                f"spack install failed (exit code {exit_code}). "
                f"Specs: {specs_str}"
            )
        print("Spack install complete")

        # Step 2: spack load + capture environment
        print(f"Loading spack packages and capturing environment...")
        env_manager = EnvironmentManager(self.jarvis)
        spack_env = env_manager.capture_spack_environment(spack_specs)

        # Step 3: Merge spack environment into pipeline environment
        self.env.update(spack_env)
        print(f"Spack environment merged ({len(spack_env)} variables updated)")

    def create(self, pipeline_name: str):
        """
        Create a new pipeline.

        :param pipeline_name: Name of the pipeline to create
        """
        self.name = pipeline_name

        # Create all three directories for the pipeline
        pipeline_config_dir = self.jarvis.get_pipeline_dir(pipeline_name)
        pipeline_shared_dir = self.jarvis.get_pipeline_shared_dir(pipeline_name)
        pipeline_private_dir = self.jarvis.get_pipeline_private_dir(pipeline_name)

        pipeline_config_dir.mkdir(parents=True, exist_ok=True)
        pipeline_shared_dir.mkdir(parents=True, exist_ok=True)
        pipeline_private_dir.mkdir(parents=True, exist_ok=True)

        # Initialize pipeline state
        self.packages = []
        self.interceptors = {}
        self.env = {}
        self.created_at = str(Path().cwd())
        self.last_loaded_file = None

        # Save pipeline configuration and environment
        self.save()

        # Set as current pipeline
        self.jarvis.set_current_pipeline(pipeline_name)

        print(f"Created pipeline: {pipeline_name}")
        print(f"Config directory: {pipeline_config_dir}")
        print(f"Shared directory: {pipeline_shared_dir}")
        print(f"Private directory: {pipeline_private_dir}")
        
    def load(self, load_type: str = None, pipeline_file: str = None):
        """
        Load pipeline from file or current configuration.
        
        :param load_type: Type of pipeline file (e.g., 'yaml')
        :param pipeline_file: Path to pipeline file
        """
        if load_type and pipeline_file:
            self._load_from_file(load_type, pipeline_file)
        elif self.name:
            self._load_from_config()
        else:
            raise ValueError("No pipeline name or file specified")
    
    def save(self):
        """
        Save pipeline configuration and environment to separate files.

        Creates two YAML files:
        - pipeline.yaml: Contains package/interceptor configuration in script format
        - environment.yaml: Contains environment variables only
        """
        if not self.name:
            raise ValueError("Pipeline name not set")

        pipeline_dir = self.jarvis.get_pipeline_dir(self.name)
        pipeline_dir.mkdir(parents=True, exist_ok=True)

        # Create pipeline configuration in the SAME format as pipeline scripts
        # This allows code reuse by using the same parsing logic
        pipeline_config = {
            'name': self.name,
            'pkgs': [],
            'interceptors': []
        }

        # Add metadata fields
        if self.created_at:
            pipeline_config['created_at'] = self.created_at
        if self.last_loaded_file:
            pipeline_config['last_loaded_file'] = self.last_loaded_file
        # Add container parameters (always save, even if empty/default)
        pipeline_config['container_image'] = self.container_image
        pipeline_config['container_engine'] = self.container_engine
        pipeline_config['container_base'] = self.container_base
        pipeline_config['container_ssh_port'] = self.container_ssh_port
        if self.container_extensions:
            pipeline_config['container_extensions'] = self.container_extensions
        if self.container_env:
            pipeline_config['container_env'] = self.container_env
        if self.container_host_path:
            pipeline_config['container_host_path'] = self.container_host_path
        if self.container_workspace:
            pipeline_config['container_workspace'] = self.container_workspace

        # Add install manager
        if self.install_manager:
            pipeline_config['install_manager'] = self.install_manager

        # Add hostfile parameter. The effective hostfile (pipeline override
        # if set, else the global jarvis hostfile) is always persisted to
        # <shared_dir>/hostfile so container mode can read it at the same
        # path the bind-mount exposes — no /tmp passthrough or external
        # bind required.
        effective_hostfile = self.hostfile or self.jarvis.hostfile
        if effective_hostfile and effective_hostfile.path:
            shared_dir = self.jarvis.get_pipeline_shared_dir(self.name)
            shared_dir.mkdir(parents=True, exist_ok=True)
            hostfile_shared_path = str(shared_dir / 'hostfile')
            effective_hostfile.save(hostfile_shared_path)
            pipeline_config['hostfile'] = hostfile_shared_path
        else:
            pipeline_config['hostfile'] = None

        # Convert packages to script format (pkg_type + config parameters)
        for pkg in self.packages:
            pkg_entry = {
                'pkg_type': pkg['pkg_type']
            }
            # Add pkg_name if different from pkg_type
            if pkg['pkg_id'] != pkg['pkg_name']:
                pkg_entry['pkg_name'] = pkg['pkg_id']

            # Add all config parameters except internal ones
            config = pkg.get('config', {})
            for key, value in config.items():
                pkg_entry[key] = value

            pipeline_config['pkgs'].append(pkg_entry)

        # Convert interceptors to script format
        for interceptor_id, interceptor_def in self.interceptors.items():
            interceptor_entry = {
                'pkg_type': interceptor_def['pkg_type']
            }
            # Add pkg_name if different from pkg_type
            if interceptor_id != interceptor_def['pkg_name']:
                interceptor_entry['pkg_name'] = interceptor_id

            # Add all config parameters
            config = interceptor_def.get('config', {})
            for key, value in config.items():
                interceptor_entry[key] = value

            pipeline_config['interceptors'].append(interceptor_entry)

        # Save pipeline configuration (same format as pipeline scripts)
        config_file = pipeline_dir / 'pipeline.yaml'
        with open(config_file, 'w') as f:
            yaml.dump(pipeline_config, f, default_flow_style=False)

        # Save environment to separate file
        env_file = pipeline_dir / 'environment.yaml'
        with open(env_file, 'w') as f:
            yaml.dump(self.env, f, default_flow_style=False)
    
    def destroy(self, pipeline_name: str = None):
        """
        Destroy a pipeline by removing its directory and configuration.
        If no pipeline name is provided, destroy the current pipeline.
        
        :param pipeline_name: Name of pipeline to destroy (optional)
        """
        # Determine which pipeline to destroy
        if pipeline_name is None:
            if not self.name:
                current_pipeline = self.jarvis.get_current_pipeline()
                if not current_pipeline:
                    print("No current pipeline to destroy. Specify a pipeline name or create/switch to one first.")
                    return
                pipeline_name = current_pipeline
            else:
                pipeline_name = self.name
                
        target_pipeline_dir = self.jarvis.get_pipeline_dir(pipeline_name)
        current_pipeline = self.jarvis.get_current_pipeline()
        is_current = (pipeline_name == current_pipeline)
        
        # Check if pipeline exists
        if not target_pipeline_dir.exists():
            print(f"Pipeline '{pipeline_name}' not found.")
            return
        
        # Try to clean packages first if pipeline is loadable
        config_file = target_pipeline_dir / 'pipeline.yaml'
        if config_file.exists():
            try:
                # Load and clean pipeline
                temp_pipeline = Pipeline(pipeline_name)
                print("Attempting to clean package data before destruction...")
                temp_pipeline.clean()
            except Exception as e:
                print(f"Warning: Could not clean packages before destruction: {e}")
        
        # Remove pipeline directory
        import shutil
        try:
            shutil.rmtree(target_pipeline_dir)
            print(f"Destroyed pipeline: {pipeline_name}")
            
            # Clear current pipeline if we destroyed it
            if is_current:
                config = self.jarvis.config.copy()
                config['current_pipeline'] = None
                self.jarvis.save_config(config)
                print("Cleared current pipeline (destroyed pipeline was active)")
                
        except Exception as e:
            print(f"Error destroying pipeline directory: {e}")
    
    def start(self):
        """Start all packages in the pipeline"""
        from jarvis_cd.util.logger import logger

        logger.pipeline(f"Starting pipeline: {self.name}")

        # Check if pipeline is configured for containerized deployment
        if self.is_containerized():
            # Container deployment mode - deploy to all nodes in hostfile
            self._start_containerized_pipeline()
        else:
            # Standard deployment mode - start each package individually
            for pkg_def in self.packages:
                try:
                    # Print BEGIN message
                    logger.success(f"[{pkg_def['pkg_type']}] [START] BEGIN")

                    pkg_instance = self._load_package_instance(pkg_def, self.env)

                    # Apply interceptors to this package before starting
                    self._apply_interceptors_to_package(pkg_instance, pkg_def)

                    if hasattr(pkg_instance, 'start'):
                        pkg_instance.start()
                    else:
                        logger.warning(f"Package {pkg_def['pkg_id']} has no start method")

                    # Propagate environment changes to next packages
                    self.env.update(pkg_instance.env)

                    # Print END message
                    logger.success(f"[{pkg_def['pkg_type']}] [START] END")

                except Exception as e:
                    logger.error(f"Error starting package {pkg_def['pkg_id']}: {e}")
                    raise RuntimeError(f"Pipeline startup failed at package '{pkg_def['pkg_id']}': {e}") from e
    
    def stop(self):
        """Stop all packages in the pipeline"""
        from jarvis_cd.util.logger import logger

        logger.pipeline(f"Stopping pipeline: {self.name}")

        # Check if pipeline is configured for containerized deployment
        if self.is_containerized():
            # Container deployment mode - stop containers on all nodes
            self._stop_containerized_pipeline()
        else:
            # Standard deployment mode - stop each package individually
            for pkg_def in reversed(self.packages):
                try:
                    # Print BEGIN message
                    logger.success(f"[{pkg_def['pkg_type']}] [STOP] BEGIN")

                    pkg_instance = self._load_package_instance(pkg_def, self.env)

                    if hasattr(pkg_instance, 'stop'):
                        pkg_instance.stop()
                    else:
                        logger.warning(f"Package {pkg_def['pkg_id']} has no stop method")

                    # Print END message
                    logger.success(f"[{pkg_def['pkg_type']}] [STOP] END")

                except Exception as e:
                    logger.error(f"Error stopping package {pkg_def['pkg_id']}: {e}")
    
    def kill(self):
        """Force kill all packages in the pipeline"""
        from jarvis_cd.util.logger import logger

        logger.pipeline(f"Killing pipeline: {self.name}")

        # Check if pipeline is configured for containerized deployment
        if self.is_containerized():
            # Container deployment mode - kill containers on all nodes
            self._kill_containerized_pipeline()
        else:
            # Standard deployment mode - kill each package individually
            for pkg_def in self.packages:
                try:
                    # Print BEGIN message
                    logger.success(f"[{pkg_def['pkg_type']}] [KILL] BEGIN")

                    pkg_instance = self._load_package_instance(pkg_def, self.env)

                    if hasattr(pkg_instance, 'kill'):
                        pkg_instance.kill()
                    else:
                        logger.warning(f"Package {pkg_def['pkg_id']} has no kill method")

                    # Print END message
                    logger.success(f"[{pkg_def['pkg_type']}] [KILL] END")

                except Exception as e:
                    logger.error(f"Error killing package {pkg_def['pkg_id']}: {e}")
    
    def status(self) -> str:
        """Get status of the pipeline and its packages"""
        from jarvis_cd.util.logger import logger, Color

        if not self.name:
            return "No pipeline loaded"

        status_info = [f"Pipeline: {self.name}"]
        status_info.append("Packages:")

        # Show status for all packages
        for pkg_def in self.packages:
            try:
                # Print BEGIN message
                logger.success(f"[{pkg_def['pkg_type']}] [STATUS] BEGIN")

                pkg_instance = self._load_package_instance(pkg_def, self.env)

                if pkg_instance and hasattr(pkg_instance, 'status'):
                    pkg_status = pkg_instance.status()
                    status_info.append(f"  {pkg_def['pkg_id']}: {pkg_status}")
                else:
                    status_info.append(f"  {pkg_def['pkg_id']}: no status method")

                # Print END message
                logger.success(f"[{pkg_def['pkg_type']}] [STATUS] END")

            except Exception as e:
                status_info.append(f"  {pkg_def['pkg_id']}: error ({e})")

        return "\n".join(status_info)
    
    def run(self, load_type: Optional[str] = None, pipeline_file: Optional[str] = None):
        """
        Run the pipeline (start all packages, then stop them).
        Optionally load a pipeline file first.

        :param load_type: Type of pipeline file to load (e.g., 'yaml')
        :param pipeline_file: Path to pipeline file to load and run
        """
        try:
            # Load pipeline file if specified
            if load_type and pipeline_file:
                self.load(load_type, pipeline_file)

            # Configure all packages before starting.
            # This runs _configure() on each package, which sets up
            # environment variables (e.g., CHI_SERVER_CONF) needed by start().
            self.configure_all_packages()

            self.start()
            logger.pipeline("Pipeline started successfully. Stopping packages...")
            self.stop()
        except Exception as e:
            logger.error(f"Error during pipeline run: {e}")
            logger.info("Attempting to stop packages...")
            try:
                self.stop()
            except Exception as stop_error:
                logger.error(f"Error during cleanup: {stop_error}")
            # Re-raise the original error after cleanup
            raise

    def configure_all_packages(self):
        """
        Configure all packages and interceptors in the pipeline.
        This method loads each package/interceptor instance and calls its configure() method,
        then updates the pipeline environment and saves the configuration.
        """
        print("Configuring interceptors and packages...")

        # Configure interceptors
        for interceptor_id, interceptor_def in self.interceptors.items():
            self._configure_package_instance(interceptor_def, "interceptor")

        # Configure packages
        for pkg_def in self.packages:
            self._configure_package_instance(pkg_def, "package")

        # Save pipeline after configuration
        self.save()
        print("Pipeline configuration saved")

    def update(self):
        """
        Update pipeline by reconfiguring all packages with their existing configurations.
        This is useful when parts of the pipeline get corrupted or the environment changes.
        """
        # Reconfigure all packages with their existing configurations
        print("Reconfiguring pipeline packages with existing configurations...")
        self.configure_all_packages()

    def _configure_package_instance(self, pkg_def: Dict[str, Any], pkg_type_label: str):
        """
        Configure a single package or interceptor instance.

        :param pkg_def: Package definition dictionary
        :param pkg_type_label: Label for logging ("package" or "interceptor")
        """
        from jarvis_cd.util.logger import logger, Color

        try:
            # Print BEGIN message
            logger.success(f"[{pkg_def['pkg_type']}] [CONFIGURE] BEGIN")

            pkg_instance = self._load_package_instance(pkg_def, self.env)
            if hasattr(pkg_instance, 'configure'):
                # Configure the package with its config
                updated_config = pkg_instance.configure(**pkg_instance.config)

                # Update pkg_def with the final config from the package
                if updated_config:
                    pkg_def['config'] = updated_config
                else:
                    pkg_def['config'] = pkg_instance.config.copy()

                # Update the package environment in the pipeline's env
                self.env.update(pkg_instance.env)

            # Print END message
            logger.success(f"[{pkg_def['pkg_type']}] [CONFIGURE] END")

        except Exception as e:
            import traceback
            logger.error(f"Error configuring {pkg_type_label} {pkg_def['pkg_id']}: {e}")
            logger.error("Full traceback:")
            traceback.print_exc()
            raise
    
    def append(self, package_spec: str, package_alias: Optional[str] = None, config_args: Optional[List[str]] = None):
        """
        Append a package to the pipeline.

        :param package_spec: Package specification (repo.pkg or just pkg)
        :param package_alias: Optional alias for the package
        :param config_args: Optional configuration arguments as command-line args (e.g., ['--out_file=/path', 'nprocs=4'])
        """
        if not self.name:
            raise ValueError("No pipeline loaded. Create one with create() first")
            
        # Parse package specification
        if '.' in package_spec:
            repo_name, pkg_name = package_spec.split('.', 1)
        else:
            # Try to find package in available repos
            pkg_name = package_spec
            full_spec = self.jarvis.find_package(pkg_name)
            if not full_spec:
                raise ValueError(f"Package not found: {pkg_name}")
            package_spec = full_spec
            
        # Determine package ID
        if package_alias:
            pkg_id = package_alias
        else:
            pkg_id = pkg_name
            
        # Check for duplicate package IDs
        existing_ids = [pkg['pkg_id'] for pkg in self.packages]
        if pkg_id in existing_ids:
            raise ValueError(f"Package ID already exists in pipeline: {pkg_id}")
            
        # Get default configuration from package
        default_config = self._get_package_default_config(package_spec)

        # Build the package entry (needed for loading package instance)
        package_entry = {
            'pkg_type': package_spec,
            'pkg_id': pkg_id,
            'pkg_name': pkg_name,
            'global_id': f"{self.name}.{pkg_id}",
            'config': default_config
        }

        # Apply configuration arguments if provided (before validation)
        if config_args:
            # Load package instance
            pkg_instance = self._load_package_instance(package_entry, self.env)

            try:
                # Use PkgArgParse to parse and convert arguments
                argparse = pkg_instance.get_argparse()
                # Parse arguments - prepend 'configure' command
                argparse.parse(['configure'] + config_args)
                converted_args = argparse.kwargs

                # Update package configuration with converted values
                package_entry['config'].update(converted_args)
            except Exception as e:
                print(f"Warning: Error parsing configuration arguments: {e}")
                # Show available configuration options
                argparse = pkg_instance.get_argparse()
                argparse.print_help('configure')

        # Validate that all required parameters have values (after applying config_args)
        self._validate_required_config(package_spec, package_entry['config'])

        # Add package to pipeline
        self.packages.append(package_entry)

        # Save updated configuration
        self.save()

        print(f"Added package {package_spec} as {pkg_id} to pipeline")
    
    def rm(self, package_spec: str):
        """
        Remove a package from the pipeline.
        
        :param package_spec: Package specification to remove (pkg_id)
        """
        # Find and remove the package
        package_found = False
        
        for i, pkg_def in enumerate(self.packages):
            if pkg_def['pkg_id'] == package_spec:
                removed_package = self.packages.pop(i)
                package_found = True
                break
                
        if not package_found:
            # List available packages to help the user
            available_ids = [pkg['pkg_id'] for pkg in self.packages]
            if available_ids:
                print(f"Package '{package_spec}' not found in pipeline.")
                print(f"Available packages: {', '.join(available_ids)}")
            else:
                print("No packages in pipeline.")
            return
            
        # Save updated configuration
        self.save()
            
        print(f"Removed package '{removed_package['pkg_id']}' ({removed_package['pkg_type']}) from pipeline '{self.name}'")
    
    def clean(self):
        """Clean all data for packages in the pipeline"""
        from jarvis_cd.util.logger import logger, Color

        logger.pipeline(f"Cleaning pipeline: {self.name}")

        # Clean each package
        for pkg_def in self.packages:
            try:
                # Print BEGIN message
                logger.success(f"[{pkg_def['pkg_type']}] [CLEAN] BEGIN")

                pkg_instance = self._load_package_instance(pkg_def, self.env)

                if hasattr(pkg_instance, 'clean'):
                    pkg_instance.clean()
                else:
                    logger.warning(f"Package {pkg_def['pkg_id']} has no clean method")

                # Print END message
                logger.success(f"[{pkg_def['pkg_type']}] [CLEAN] END")

            except Exception as e:
                logger.error(f"Error cleaning package {pkg_def['pkg_id']}: {e}")
    
    def configure_package(self, pkg_id: str, config_args: List[str]):
        """
        Configure a specific package in the pipeline.

        :param pkg_id: Package ID to configure
        :param config_args: Configuration arguments as command-line args (e.g., ['--nprocs', '4', 'block=32m'])
        """
        # Find package in pipeline
        pkg_def = None
        for pkg in self.packages:
            if pkg['pkg_id'] == pkg_id:
                pkg_def = pkg
                break

        if not pkg_def:
            raise ValueError(f"Package not found: {pkg_id}")

        # Load package instance
        pkg_instance = self._load_package_instance(pkg_def, self.env)

        try:
            # Use PkgArgParse to parse and convert arguments
            argparse = pkg_instance.get_argparse()

            # Get defaults by parsing with no user args
            argparse.parse(['configure'])
            defaults = argparse.kwargs.copy()

            # Parse with user args
            argparse.parse(['configure'] + config_args)
            converted_args = argparse.kwargs

            # Only keep args the user explicitly provided (differ from defaults)
            explicit_args = {k: v for k, v in converted_args.items()
                            if k not in defaults or defaults[k] != v}

            # Update package configuration with only explicit values
            pkg_def['config'].update(explicit_args)

            # Configure the package instance
            if hasattr(pkg_instance, 'configure'):
                pkg_instance.configure(**explicit_args)
                print(f"Configured package {pkg_id} successfully")
            else:
                print(f"Package {pkg_id} has no configure method")

            # Save updated pipeline
            self.save()
            print(f"Saved configuration for {pkg_id}")

        except Exception as e:
            print(f"Error configuring package {pkg_id}: {e}")
            # Show available configuration options
            argparse = pkg_instance.get_argparse()
            argparse.print_help('configure')
    
    def show_package_readme(self, pkg_id: str):
        """
        Show README for a specific package in the pipeline.
        
        :param pkg_id: Package ID to show README for
        """
        # Find package in pipeline
        pkg_def = None
        for pkg in self.packages:
            if pkg['pkg_id'] == pkg_id:
                pkg_def = pkg
                break
                
        if not pkg_def:
            raise ValueError(f"Package not found: {pkg_id}")
        
        # Load package instance and delegate to it
        try:
            pkg_instance = self._load_package_instance(pkg_def, self.env)
            pkg_instance.show_readme()
        except Exception as e:
            print(f"Error showing README for package {pkg_id}: {e}")
    
    def show_package_paths(self, pkg_id: str, path_flags: Dict[str, bool]):
        """
        Show directory paths for a specific package in the pipeline.
        
        :param pkg_id: Package ID to show paths for
        :param path_flags: Dictionary of path flags to show
        """
        # Find package in pipeline
        pkg_def = None
        for pkg in self.packages:
            if pkg['pkg_id'] == pkg_id:
                pkg_def = pkg
                break
                
        if not pkg_def:
            raise ValueError(f"Package not found: {pkg_id}")
        
        # Load package instance and delegate to it
        try:
            pkg_instance = self._load_package_instance(pkg_def, self.env)
            pkg_instance.show_paths(path_flags)
        except Exception as e:
            print(f"Error showing paths for package {pkg_id}: {e}")
    
    def _load_from_config(self):
        """
        Load pipeline from its configuration files.

        Loads from two separate files:
        - pipeline.yaml: Contains package/interceptor configuration in script format
        - environment.yaml: Contains environment variables only
        """
        pipeline_dir = self.jarvis.get_pipeline_dir(self.name)
        config_file = pipeline_dir / 'pipeline.yaml'

        if not config_file.exists():
            raise FileNotFoundError(f"Pipeline configuration not found: {config_file}")

        # Load pipeline configuration (in script format)
        with open(config_file, 'r') as f:
            pipeline_config = yaml.safe_load(f)

        # Extract metadata
        self.created_at = pipeline_config.get('created_at')
        self.last_loaded_file = pipeline_config.get('last_loaded_file')

        # Load container parameters
        self.container_image = pipeline_config.get('container_image', '')
        self.container_engine = pipeline_config.get('container_engine', 'podman')
        self.container_base = pipeline_config.get('container_base', 'iowarp/iowarp-build:latest')
        self.container_ssh_port = pipeline_config.get('container_ssh_port', 2222)
        self.container_extensions = pipeline_config.get('container_extensions', {})
        self.container_env = pipeline_config.get('container_env', {})
        self.container_host_path = pipeline_config.get('container_host_path', '')
        self.container_workspace = pipeline_config.get('container_workspace', '')

        # Load install manager
        self.install_manager = pipeline_config.get('install_manager', None)

        # Load hostfile parameter (None means use global jarvis hostfile)
        hostfile_path = pipeline_config.get('hostfile')
        if hostfile_path:
            self.hostfile = Hostfile(path=os.path.expandvars(hostfile_path))
        else:
            self.hostfile = None

        # Initialize packages and interceptors
        self.packages = []
        self.interceptors = {}

        # Process interceptors from script format
        interceptors_list = pipeline_config.get('interceptors', [])
        for interceptor_def in interceptors_list:
            interceptor_id = interceptor_def.get('pkg_name', interceptor_def['pkg_type'].split('.')[-1])
            interceptor_entry = self._process_package_definition(interceptor_def, interceptor_id)
            self.interceptors[interceptor_id] = interceptor_entry

        # Process packages from script format
        for pkg_def in pipeline_config.get('pkgs', []):
            pkg_id = pkg_def.get('pkg_name', pkg_def['pkg_type'].split('.')[-1])
            package_entry = self._process_package_definition(pkg_def, pkg_id)
            self.packages.append(package_entry)

        # Load environment from separate file
        env_file = pipeline_dir / 'environment.yaml'
        if env_file.exists():
            with open(env_file, 'r') as f:
                env_config = yaml.safe_load(f)
                if env_config:
                    self.env = env_config
                else:
                    self.env = {}
        else:
            self.env = {}
    
    def _load_from_file(self, load_type: str, pipeline_file: str):
        """Load pipeline from a file"""
        if load_type != 'yaml':
            raise ValueError(f"Unsupported pipeline file type: {load_type}")
            
        pipeline_file = Path(pipeline_file)
        if not pipeline_file.exists():
            raise FileNotFoundError(f"Pipeline file not found: {pipeline_file}")
            
        # Load pipeline definition
        with open(pipeline_file, 'r') as f:
            pipeline_def = yaml.safe_load(f)
            
        self.name = pipeline_def.get('name', pipeline_file.stem)
        
        # Handle environment - can be a string (named env) or missing (auto-build)
        # Inline dictionaries are NOT supported
        env_field = pipeline_def.get('env')

        if env_field is None:
            # No env field defined - automatically build environment
            try:
                from jarvis_cd.core.environment import EnvironmentManager
                env_manager = EnvironmentManager(self.jarvis)
                self.env = env_manager._capture_current_environment()
                print(f"Auto-built environment with {len(self.env)} variables (no 'env' field in pipeline)")
            except Exception as e:
                print(f"Warning: Could not auto-build environment: {e}")
                self.env = {}
        elif isinstance(env_field, str):
            # Reference to named environment (deprecated)
            print(f"Warning: String env references are deprecated. Use inline dict overrides instead.")
            env_name = env_field
            try:
                from jarvis_cd.core.environment import EnvironmentManager
                env_manager = EnvironmentManager(self.jarvis)
                self.env = env_manager.load_named_environment(env_name)
            except Exception as e:
                # Named environment doesn't exist - build it automatically
                print(f"Named environment '{env_name}' does not exist. Building it now...")
                try:
                    from jarvis_cd.core.environment import EnvironmentManager
                    env_manager = EnvironmentManager(self.jarvis)
                    # Build the named environment from current environment (no additional args)
                    env_manager.build_named_environment(env_name, [])
                    # Now load the newly created environment
                    self.env = env_manager.load_named_environment(env_name)
                    print(f"Built named environment '{env_name}' with {len(self.env)} variables")
                except Exception as build_error:
                    print(f"Warning: Could not build named environment '{env_name}': {build_error}")
                    self.env = {}
        elif isinstance(env_field, dict):
            # Inline dict: auto-capture environment, then overlay user overrides
            try:
                from jarvis_cd.core.environment import EnvironmentManager
                env_manager = EnvironmentManager(self.jarvis)
                self.env = env_manager._capture_current_environment()
            except Exception as e:
                print(f"Warning: Could not auto-build environment: {e}")
                self.env = {}
            # Apply user overrides from YAML
            self.env.update(env_field)
            print(f"Built environment with {len(self.env)} variables ({len(env_field)} overrides from pipeline YAML)")
        else:
            raise ValueError(
                f"Invalid 'env' field type: {type(env_field).__name__}. "
                "The 'env' field must be either a string (named environment) or omitted (auto-build)."
            )
        
        # Initialize other attributes
        self.created_at = str(Path().cwd())
        self.last_loaded_file = str(pipeline_file.absolute())
        self.packages = []
        self.interceptors = {}  # Store pipeline-level interceptors by name

        # Load container parameters
        self.container_image = pipeline_def.get('container_image', '')
        self.container_engine = pipeline_def.get('container_engine', 'podman')
        self.container_base = pipeline_def.get('container_base', 'iowarp/iowarp-build:latest')
        self.container_ssh_port = pipeline_def.get('container_ssh_port', 2222)
        self.container_extensions = pipeline_def.get('container_extensions', {})
        self.container_env = pipeline_def.get('container_env', {})
        self.container_host_path = pipeline_def.get('container_host_path', '')
        self.container_workspace = pipeline_def.get('container_workspace', '')
        self.container_gpu = pipeline_def.get('container_gpu', False)

        # Load install manager
        self.install_manager = pipeline_def.get('install_manager', None)

        # Load hostfile parameter (None means use global jarvis hostfile)
        hostfile_path = pipeline_def.get('hostfile')
        if hostfile_path:
            self.hostfile = Hostfile(path=os.path.expandvars(hostfile_path))
        else:
            self.hostfile = None

        # Process interceptors
        interceptors_list = pipeline_def.get('interceptors', [])
        for interceptor_def in interceptors_list:
            interceptor_id = interceptor_def.get('pkg_name', interceptor_def['pkg_type'].split('.')[-1])
            interceptor_entry = self._process_package_definition(interceptor_def, interceptor_id)
            self.interceptors[interceptor_id] = interceptor_entry

        # Process packages
        for pkg_def in pipeline_def.get('pkgs', []):
            pkg_id = pkg_def.get('pkg_name', pkg_def['pkg_type'])
            package_entry = self._process_package_definition(pkg_def, pkg_id)
            self.packages.append(package_entry)
        
        # Validate that interceptor and package IDs are unique
        self._validate_unique_ids()

        # Derive deploy_mode from install_manager and propagate to all packages
        self._propagate_deploy_mode()

        # Install phase: spack or container based on install_manager
        if self.install_manager == 'spack':
            self._install_spack_packages()
        elif self.install_manager == 'container':
            if not self.container_image:
                self._build_pipeline_container()
                self.container_image = self.name

        # Save pipeline configuration and environment
        self.save()

        # Generate container compose file if this is a containerized pipeline
        if self.install_manager == 'container' and self.is_containerized():
            print(f"Generating container configuration files...")
            self._generate_pipeline_container_yaml()
            # Apptainer runs commands directly via 'apptainer exec .sif';
            # it has no compose file or long-running container daemons.
            if self.container_engine != 'apptainer':
                self._generate_pipeline_compose_file()

        # Set as current pipeline
        self.jarvis.set_current_pipeline(self.name)

        print(f"Loaded pipeline: {self.name}")
        print(f"Packages: {[pkg['pkg_id'] for pkg in self.packages]}")
    
    def _load_package_instance(self, pkg_def: Dict[str, Any], pipeline_env: Optional[Dict[str, str]] = None):
        """
        Load a package instance from package definition.
        
        :param pkg_def: Package definition dictionary
        :param pipeline_env: Pipeline environment variables
        :return: Package instance
        """
        from jarvis_cd.core.pkg import Pkg
        
        pkg_type = pkg_def['pkg_type']
        
        # Find package class
        if '.' in pkg_type:
            # Full specification like "builtin.ior"
            import_parts = pkg_type.split('.')
            repo_name = import_parts[0]
            pkg_name = import_parts[1]
        else:
            # Just package name, search in repos
            full_spec = self.jarvis.find_package(pkg_type)
            if not full_spec:
                raise ValueError(f"Package not found: {pkg_type}")
            import_parts = full_spec.split('.')
            repo_name = import_parts[0]
            pkg_name = import_parts[1]
            
        # Determine class name (convert snake_case and kebab-case to PascalCase)
        import re
        class_name = ''.join(word.capitalize() for word in re.split(r'[_-]', pkg_name))
        
        # Load class
        if repo_name == 'builtin':
            repo_path = str(self.jarvis.get_builtin_repo_path())
        else:
            # Find repo path in registered repos
            repo_path = None
            for registered_repo in self.jarvis.repos['repos']:
                if Path(registered_repo).name == repo_name:
                    repo_path = registered_repo
                    break
                    
            if not repo_path:
                raise ValueError(f"Repository not found: {repo_name}")
                
        import_str = f"{repo_name}.{pkg_name}.pkg"
        try:
            pkg_class = load_class(import_str, repo_path, class_name)
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            raise ValueError(
                f"Failed to load package '{pkg_type}':\n"
                f"  Repository: {repo_name}\n"
                f"  Package: {pkg_name}\n"
                f"  Repo path: {repo_path}\n"
                f"  Import string: {import_str}\n"
                f"  Class name: {class_name}\n"
                f"  Error: {e}\n"
                f"  Traceback:\n{error_details}"
            )

        if not pkg_class:
            raise ValueError(f"Package class not found: {class_name} in {import_str}")

        # Create instance with pipeline context
        try:
            pkg_instance = pkg_class(pipeline=self)
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            raise ValueError(
                f"Failed to instantiate package '{pkg_type}':\n"
                f"  Class: {class_name}\n"
                f"  Error during __init__: {e}\n"
                f"  Traceback:\n{error_details}"
            )

        # Set basic attributes
        pkg_instance.pkg_type = pkg_def['pkg_type']
        pkg_instance.pkg_id = pkg_def['pkg_id']
        pkg_instance.global_id = pkg_def['global_id']

        # Initialize directories now that pkg_id is set
        pkg_instance._ensure_directories()

        # Set configuration
        base_config = pkg_def.get('config', {})
        base_config.setdefault('do_dbg', False)
        base_config.setdefault('dbg_port', 50000)
        pkg_instance.config = base_config
            
        # Set up environment variables - mod_env is exact replica of env plus LD_PRELOAD
        if pipeline_env is None:
            pipeline_env = {}

        # env contains everything except LD_PRELOAD
        pkg_instance.env = {k: v for k, v in pipeline_env.items() if k != 'LD_PRELOAD'}

        # mod_env is exact replica of env plus LD_PRELOAD (if it exists)
        pkg_instance.mod_env = pkg_instance.env.copy()
        if 'LD_PRELOAD' in pipeline_env:
            pkg_instance.mod_env['LD_PRELOAD'] = pipeline_env['LD_PRELOAD']

        return pkg_instance
    
    def _process_package_definition(self, pkg_def: Dict[str, Any], pkg_id: str) -> Dict[str, Any]:
        """
        Process a package definition from YAML, merging YAML config with defaults.

        :param pkg_def: Package definition from YAML
        :param pkg_id: Package ID to use
        :return: Complete package entry with merged configuration
        """
        pkg_type = pkg_def['pkg_type']

        # Resolve pkg_type to full specification (repo.package) if not already specified
        if '.' not in pkg_type:
            resolved_type = self.jarvis.find_package(pkg_type)
            if resolved_type:
                pkg_type = resolved_type
            # If not found, keep original (will fail later during loading)

        # Get default configuration from package
        default_config = self._get_package_default_config(pkg_type)

        # Extract config from YAML
        yaml_config = {k: v for k, v in pkg_def.items()
                      if k not in ['pkg_type', 'pkg_name']}

        # Merge YAML config on top of defaults
        merged_config = default_config.copy()
        merged_config.update(yaml_config)

        return {
            'pkg_type': pkg_type,
            'pkg_id': pkg_id,
            'pkg_name': pkg_type.split('.')[-1],
            'global_id': f"{self.name}.{pkg_id}",
            'config': merged_config
        }

    def _get_package_default_config(self, package_spec: str) -> Dict[str, Any]:
        """
        Get default configuration values for a package by parsing with PkgArgParse.
        Equivalent to calling 'configure' with no parameters.
        """
        try:
            # Create a temporary package definition to load the package
            temp_pkg_def = {
                'pkg_type': package_spec,
                'pkg_id': 'temp',
                'pkg_name': package_spec.split('.')[-1],
                'global_id': 'temp.temp',
                'config': {}
            }

            # Load package instance
            pkg_instance = self._load_package_instance(temp_pkg_def)

            # Use PkgArgParse to get defaults by parsing 'configure' with no args
            argparse = pkg_instance.get_argparse()
            argparse.parse(['configure'])
            default_config = argparse.kwargs

            return default_config

        except Exception as e:
            # Package loading failure should be fatal - cannot add package to pipeline
            raise ValueError(f"Failed to load package '{package_spec}': {e}")

        return {}
    
    def _validate_unique_ids(self):
        """
        Validate that interceptor IDs and package IDs are unique within the pipeline.
        """
        # Get all package IDs
        package_ids = {pkg['pkg_id'] for pkg in self.packages}
        
        # Get all interceptor IDs
        interceptor_ids = set(self.interceptors.keys())
        
        # Check for conflicts
        conflicts = package_ids & interceptor_ids
        if conflicts:
            conflict_list = ', '.join(conflicts)
            raise ValueError(f"ID conflicts between packages and interceptors: {conflict_list}. "
                           f"Package and interceptor IDs must be unique within the pipeline.")
    
    def _validate_required_config(self, package_spec: str, config: Dict[str, Any]):
        """
        Validate that all required configuration parameters have values.
        
        :param package_spec: Package specification 
        :param config: Configuration dictionary
        :raises ValueError: If required parameters are missing or None
        """
        try:
            # Create a temporary package definition to load the package
            temp_pkg_def = {
                'pkg_type': package_spec,
                'pkg_id': 'temp',
                'pkg_name': package_spec.split('.')[-1],
                'global_id': 'temp.temp',
                'config': {}
            }
            
            # Load package instance
            pkg_instance = self._load_package_instance(temp_pkg_def)
            
            # Get configuration menu to check for required parameters
            if hasattr(pkg_instance, 'configure_menu'):
                config_menu = pkg_instance.configure_menu()
                if config_menu:
                    missing_required = []
                    for menu_item in config_menu:
                        name = menu_item.get('name')
                        default_value = menu_item.get('default')
                        
                        # If parameter has no default and is not provided in config, it's required
                        if name and default_value is None and (name not in config or config[name] is None):
                            missing_required.append(name)
                    
                    if missing_required:
                        raise ValueError(f"Missing required configuration parameters for {package_spec}: {', '.join(missing_required)}")
                        
        except Exception as e:
            if "Missing required configuration parameters" in str(e):
                raise  # Re-raise our validation error
            # Other errors during validation are not fatal
            print(f"Warning: Could not validate configuration for {package_spec}: {e}")
    
    def _apply_interceptors_to_package(self, pkg_instance, pkg_def):
        """
        Apply interceptors to a package instance during pipeline start.

        :param pkg_instance: The package instance to apply interceptors to
        :param pkg_def: The package definition from pipeline configuration
        """
        from jarvis_cd.util.logger import logger, Color

        # Get interceptors list from package configuration
        interceptors_list = pkg_def.get('config', {}).get('interceptors', [])

        if not interceptors_list:
            return

        logger.warning(f"Applying {len(interceptors_list)} interceptors to {pkg_def['pkg_id']}")

        for interceptor_name in interceptors_list:
            try:
                # Find interceptor in pipeline-level interceptors
                if interceptor_name not in self.interceptors:
                    logger.error(f"Warning: Interceptor '{interceptor_name}' not found in pipeline interceptors")
                    continue

                interceptor_def = self.interceptors[interceptor_name]

                # Print BEGIN message with full interceptor type
                logger.success(f"[{interceptor_def['pkg_type']}] [MODIFY_ENV] BEGIN")

                # Load interceptor instance
                interceptor_instance = self._load_package_instance(interceptor_def, self.env)

                # Verify it's an interceptor and has modify_env method
                if not hasattr(interceptor_instance, 'modify_env'):
                    logger.error(f"Warning: Package '{interceptor_name}' does not have modify_env() method")
                    continue

                # Share the same mod_env reference between interceptor and package
                interceptor_instance.mod_env = pkg_instance.mod_env
                interceptor_instance.env = pkg_instance.env

                # Call modify_env on the interceptor to modify the shared environment
                interceptor_instance.modify_env()

                # The mod_env is shared, so changes are automatically applied to the package

                # Print END message
                logger.success(f"[{interceptor_def['pkg_type']}] [MODIFY_ENV] END")

            except Exception as e:
                logger.error(f"Error applying interceptor '{interceptor_name}': {e}")

    def _build_pipeline_container(self):
        """
        Build pipeline container images using a single long-running build
        container.

        Flow:
        1. Check if the deploy image already exists (skip if cached).
        2. Start a build container from container_base.
        3. For each package, exec its build.sh script inside the container.
        4. Commit the build container as a build image.
        5. Build deploy image(s) from Dockerfile.deploy (copies from build image).
        6. Stop and remove the build container.
        """
        from jarvis_cd.shell import Exec, LocalExecInfo
        from jarvis_cd.core.pkg import Pkg

        deploy_image_name = self.name
        pipeline_shared_dir = self.jarvis.get_pipeline_shared_dir(self.name)
        pipeline_shared_dir.mkdir(parents=True, exist_ok=True)

        # Determine build engine (apptainer uses docker/podman as intermediate)
        build_engine = self.container_engine
        if build_engine == 'apptainer':
            import shutil
            build_engine = 'docker' if shutil.which('docker') else 'podman'

        # Check if deploy image already exists — skip build if cached
        all_cached = all(
            pkg_def.get('config', {}).get('container_cache', True)
            for pkg_def in self.packages
        )
        if all_cached and Pkg._image_exists(
                self.container_engine, deploy_image_name,
                shared_dir=str(pipeline_shared_dir)):
            print(f"Deploy image '{deploy_image_name}' already exists, skipping build")
            return

        # On HPC without docker/podman, build directly with apptainer
        # from a .def file that embeds all package build scripts.
        if self.container_engine == 'apptainer' and not shutil.which(build_engine):
            self._build_apptainer_native(deploy_image_name, pipeline_shared_dir)
            return

        # -----------------------------------------------------------------
        # Phase 1: Build — run each package's build.sh in a single container
        # -----------------------------------------------------------------
        build_container_name = f'jarvis-build-{self.name}'
        build_image_name = f'jarvis-build-{self.name}'
        base_image = self.container_base

        print(f"Starting build container from {base_image}...")
        start_cmd = (
            f"{build_engine} run -d --name {build_container_name} "
            f"--network=host {base_image} sleep infinity"
        )
        result = Exec(start_cmd, LocalExecInfo()).run()
        if result.exit_code.get('localhost', 1) != 0:
            raise RuntimeError(
                f"Failed to start build container '{build_container_name}'"
            )

        try:
            has_build_content = False
            for pkg_def in self.packages:
                pkg_instance = self._load_package_instance(pkg_def, self.env)
                pkg_instance.config['deploy_mode'] = pkg_def.get(
                    'config', {}).get('deploy_mode', 'default')

                build_result = pkg_instance._build_phase()
                if not build_result:
                    continue
                script_content, build_suffix = build_result
                if not script_content:
                    continue

                pkg_instance._build_suffix = build_suffix
                pkg_name_raw = pkg_def['pkg_type'].split('.')[-1].replace('_', '-')
                pkg_deploy_name = f'jarvis-deploy-{pkg_name_raw}'
                if build_suffix:
                    pkg_deploy_name = f'{pkg_deploy_name}-{build_suffix}'
                use_cache = pkg_def.get('config', {}).get('container_cache', True)

                # If a cached deploy image exists for this package, inject
                # its artifacts into the build container instead of rebuilding.
                # This makes Library packages (hdf5, adios2, etc.) available
                # to later packages without re-compiling them.
                if use_cache and Pkg._image_exists(build_engine, pkg_deploy_name):
                    print(f"Injecting cached '{pkg_deploy_name}' into build container...")
                    temp_name = f'jarvis-inject-{pkg_name_raw}'
                    Exec(f"{build_engine} rm -f {temp_name}",
                         LocalExecInfo(hide_output=True)).run()
                    Exec(f"{build_engine} create --name {temp_name} {pkg_deploy_name}",
                         LocalExecInfo(hide_output=True)).run()
                    for src_path in ['/usr/local/.', '/opt/.']:
                        dest_path = src_path.rstrip('.')
                        Exec(f"{build_engine} cp {temp_name}:{src_path} "
                             f"{build_container_name}:{dest_path}",
                             LocalExecInfo(hide_output=True)).run()
                    Exec(f"{build_engine} rm {temp_name}",
                         LocalExecInfo(hide_output=True)).run()
                    Exec(f"{build_engine} exec {build_container_name} ldconfig",
                         LocalExecInfo(hide_output=True)).run()
                    has_build_content = True
                    continue

                has_build_content = True
                pkg_name = pkg_def['pkg_name']

                # Write build script to shared dir
                script_path = pipeline_shared_dir / f'build-{pkg_name}.sh'
                with open(script_path, 'w') as f:
                    f.write(script_content)

                # Copy script into build container
                cp_cmd = (
                    f"{build_engine} cp {script_path} "
                    f"{build_container_name}:/tmp/build-{pkg_name}.sh"
                )
                Exec(cp_cmd, LocalExecInfo()).run()

                # Execute build script inside the container
                print(f"Building {pkg_name} in container...")
                exec_cmd = (
                    f"{build_engine} exec {build_container_name} "
                    f"bash /tmp/build-{pkg_name}.sh"
                )
                result = Exec(exec_cmd, LocalExecInfo()).run()
                if result.exit_code.get('localhost', 1) != 0:
                    raise RuntimeError(
                        f"Build script failed for '{pkg_name}'. "
                        f"Script: {script_path}"
                    )
                print(f"Build complete: {pkg_name}")

            # Commit the build container as an image
            if has_build_content:
                print(f"Committing build container as {build_image_name}...")
                commit_cmd = (
                    f"{build_engine} commit {build_container_name} {build_image_name}"
                )
                result = Exec(commit_cmd, LocalExecInfo()).run()
                if result.exit_code.get('localhost', 1) != 0:
                    raise RuntimeError(
                        f"Failed to commit build container as '{build_image_name}'"
                    )
        finally:
            # Always stop and remove the build container
            Exec(f"{build_engine} rm -f {build_container_name}",
                 LocalExecInfo(hide_output=True)).run()

        # -----------------------------------------------------------------
        # Phase 2: Deploy — build deploy image(s) from Dockerfile.deploy
        # -----------------------------------------------------------------

        per_pkg_deploy_images = []

        for pkg_def in self.packages:
            pkg_instance = self._load_package_instance(pkg_def, self.env)
            pkg_instance.config['deploy_mode'] = pkg_def.get(
                'config', {}).get('deploy_mode', 'default')
            # Point ##BUILD_IMAGE## at the committed build image
            pkg_instance._build_suffix = ''

            deploy_result = pkg_instance._build_deploy_phase()
            if not deploy_result:
                continue
            deploy_content, deploy_suffix = deploy_result
            if not deploy_content:
                continue

            # Per-package deploy image name — stable, reusable across pipelines
            pkg_name = pkg_def['pkg_type'].split('.')[-1].replace('_', '-')
            pkg_deploy_name = f'jarvis-deploy-{pkg_name}'
            if deploy_suffix:
                pkg_deploy_name = f'{pkg_deploy_name}-{deploy_suffix}'

            # Replace ##BUILD_IMAGE## references with the committed image name
            deploy_content = deploy_content.replace(
                pkg_instance.build_image_name(), build_image_name)

            # Check if this per-package deploy image is already cached
            use_cache = pkg_def.get('config', {}).get('container_cache', True)
            if use_cache and Pkg._image_exists(build_engine, pkg_deploy_name):
                print(f"Deploy image '{pkg_deploy_name}' cached, skipping build")
            else:
                deploy_df_path = pipeline_shared_dir / f'deploy-{pkg_name}.Dockerfile'
                with open(deploy_df_path, 'w') as f:
                    f.write(deploy_content)
                print(f"Building per-package deploy image: {pkg_deploy_name}")
                build_cmd = (
                    f"{build_engine} build --network=host -t {pkg_deploy_name} "
                    f"-f {deploy_df_path} {pipeline_shared_dir}"
                )
                result = Exec(build_cmd, LocalExecInfo()).run()
                if result.exit_code.get('localhost', 1) != 0:
                    raise RuntimeError(
                        f"Failed to build deploy image '{pkg_deploy_name}'"
                    )

            per_pkg_deploy_images.append(pkg_deploy_name)

        if not per_pkg_deploy_images:
            print("Warning: No deploy Dockerfile content from any package")
            return

        # Merge per-package deploy images into a single pipeline image
        if len(per_pkg_deploy_images) == 1:
            # Single package — just tag it as the pipeline image
            tag_cmd = f"{build_engine} tag {per_pkg_deploy_images[0]} {deploy_image_name}"
            Exec(tag_cmd, LocalExecInfo()).run()
        else:
            # Multiple packages — overlay files from all per-package images.
            # Also pull /usr/lib/x86_64-linux-gnu so apt-installed runtime
            # shared libs (e.g. libgomp1 for LAMMPS OpenMP) follow their
            # binaries into the merged image. All deploy bases derive from
            # the same distro, so the overlay is a compatible superset.
            lines = [f"FROM {per_pkg_deploy_images[0]}"]
            for img in per_pkg_deploy_images[1:]:
                lines.append(f"COPY --from={img} /usr/local /usr/local")
                lines.append(f"COPY --from={img} /usr/lib/x86_64-linux-gnu /usr/lib/x86_64-linux-gnu")
                lines.append(f"COPY --from={img} /opt /opt")
            lines.append("RUN ldconfig")
            lines.append('CMD ["/bin/bash"]')
            deploy_dockerfile = "\n".join(lines)

            deploy_dockerfile_path = pipeline_shared_dir / 'deploy.Dockerfile'
            with open(deploy_dockerfile_path, 'w') as f:
                f.write(deploy_dockerfile)

            print(f"Building merged deploy image: {deploy_image_name}")
            deploy_cmd = (
                f"{build_engine} build --network=host -t {deploy_image_name} "
                f"-f {deploy_dockerfile_path} {pipeline_shared_dir}"
            )
            result = Exec(deploy_cmd, LocalExecInfo()).run()
            if result.exit_code.get('localhost', 1) != 0:
                raise RuntimeError(
                    f"Failed to build deploy image '{deploy_image_name}'"
                )

        # Convert to SIF for apptainer
        if self.container_engine == 'apptainer':
            sif_path = pipeline_shared_dir / f'{deploy_image_name}.sif'
            print(f"Converting to Apptainer SIF: {sif_path}")
            from jarvis_cd.shell.container_compose_exec import ApptainerBuildExec
            ApptainerBuildExec(
                deploy_image_name, str(sif_path),
                LocalExecInfo(), source='docker-daemon'
            ).run()
            print(f"Apptainer SIF ready: {sif_path}")
        else:
            print(f"Deploy image ready: {deploy_image_name}")

        # Clean up the committed build image (deploy has everything)
        Exec(f"{build_engine} rmi {build_image_name}",
             LocalExecInfo(hide_output=True)).run()

    def _build_apptainer_native(self, deploy_image_name, pipeline_shared_dir):
        """
        Build a .sif directly with 'apptainer build' when docker/podman
        are not available (typical on HPC).  Generates an Apptainer
        definition file that embeds all package build scripts in %post.
        """
        from jarvis_cd.shell import Exec, LocalExecInfo

        base_image = self.container_base
        def_path = pipeline_shared_dir / f'{deploy_image_name}.def'
        sif_path = pipeline_shared_dir / f'{deploy_image_name}.sif'

        # Collect build scripts from all packages
        build_scripts = []
        env_paths = []
        for pkg_def in self.packages:
            pkg_instance = self._load_package_instance(pkg_def, self.env)
            pkg_instance.config['deploy_mode'] = pkg_def.get(
                'config', {}).get('deploy_mode', 'default')

            build_result = pkg_instance._build_phase()
            if not build_result:
                continue
            script_content, _ = build_result
            if not script_content:
                continue
            build_scripts.append(f'# --- Build: {pkg_def["pkg_name"]} ---')
            build_scripts.append(script_content)

            # Collect install paths for %environment
            pkg_name = pkg_def['pkg_type'].split('.')[-1]
            env_paths.append(f'/opt/{pkg_name}/install/bin')

        if not build_scripts:
            print("Warning: No build scripts from any package")
            return

        # Generate .def file
        env_path_str = ':'.join(env_paths + ['$PATH'])
        env_ld_str = ':'.join(
            f'/opt/{p["pkg_type"].split(".")[-1]}/install/lib'
            for p in self.packages) + ':$LD_LIBRARY_PATH'

        def_content = f"Bootstrap: docker\nFrom: {base_image}\n\n"
        def_content += "%post\n"
        def_content += '\n'.join(build_scripts)
        def_content += "\n\n%environment\n"
        def_content += f"export PATH={env_path_str}\n"
        def_content += f"export LD_LIBRARY_PATH={env_ld_str}\n"

        with open(def_path, 'w') as f:
            f.write(def_content)

        print(f"Building Apptainer SIF from definition: {def_path}")
        build_cmd = f"apptainer build --fakeroot {sif_path} {def_path}"
        result = Exec(build_cmd, LocalExecInfo()).run()
        if result.exit_code.get('localhost', 1) != 0:
            raise RuntimeError(
                f"Apptainer build failed. Definition: {def_path}"
            )
        print(f"Apptainer SIF ready: {sif_path}")

    def _generate_pipeline_container_yaml(self):
        """
        Generate pipeline-wide YAML configuration file for use inside containers.
        This includes all packages and interceptors in the pipeline.

        :return: Path to generated YAML file
        """
        import yaml
        from pathlib import Path

        # Create pipeline configuration with all packages
        pipeline_config = {
            'name': f'{self.name}_container',
            'pkgs': []
        }

        # Add all packages (excluding 'deploy' config)
        for pkg_def in self.packages:
            pkg_entry = {'pkg_type': pkg_def['pkg_type']}
            for key, value in pkg_def['config'].items():
                if key not in ['deploy', 'deploy_mode', 'deploy_ssh_port']:
                    pkg_entry[key] = value
            pipeline_config['pkgs'].append(pkg_entry)

        # Add interceptors if any
        if self.interceptors:
            pipeline_config['interceptors'] = []
            for interceptor_name, interceptor_def in self.interceptors.items():
                interceptor_entry = {'pkg_type': interceptor_def['pkg_type']}
                for key, value in interceptor_def.get('config', {}).items():
                    if key not in ['deploy', 'deploy_mode', 'deploy_ssh_port']:
                        interceptor_entry[key] = value
                pipeline_config['interceptors'].append(interceptor_entry)

        # Write to shared directory
        shared_dir = self.jarvis.get_pipeline_shared_dir(self.name)
        yaml_path = shared_dir / 'pipeline.yaml'
        with open(yaml_path, 'w') as f:
            yaml.dump(pipeline_config, f, default_flow_style=False)

        print(f"Generated pipeline YAML: {yaml_path}")
        return yaml_path

    def _generate_pipeline_compose_file(self):
        """
        Generate pipeline-specific docker-compose file that uses the global container image.
        This compose file is stored in the pipeline's shared directory.

        :return: Path to generated compose file
        """
        import yaml
        import os

        shared_dir = self.jarvis.get_pipeline_shared_dir(self.name)
        compose_path = shared_dir / 'docker-compose.yaml'

        container_name = f"{self.name}_container"

        # Use pipeline-level SSH port configuration
        ssh_port = self.container_ssh_port

        # Path remapping for Docker-in-Docker / devcontainer environments.
        # When container_host_path and container_workspace are set, the
        # workspace is bind-mounted from a different path on the Docker host.
        # Volume mounts in docker-compose must use host paths.
        docker_host_prefix = self.container_host_path
        workspace_root = self.container_workspace
        if docker_host_prefix and workspace_root and os.path.isdir(workspace_root):
            def to_host_path(path_str):
                """Remap a workspace path to the Docker host path."""
                if path_str.startswith(workspace_root):
                    return docker_host_prefix + path_str[len(workspace_root):]
                return path_str
        else:
            def to_host_path(path_str):
                return path_str

        # Container entrypoint: start SSH and sleep forever.
        # The host-side jarvis orchestrates packages by SSH/MPI-ing into
        # the containers — no jarvis installation needed inside them.
        # In DinD environments, the home directory is on the overlay
        # filesystem — copy SSH keys to the workspace so sibling
        # containers can access them.
        ssh_dir = os.path.expanduser('~/.ssh')
        if docker_host_prefix and workspace_root and os.path.isdir(workspace_root) \
                and not ssh_dir.startswith(workspace_root):
            docker_ssh_dir = os.path.join(workspace_root, '.ssh-host')
            if os.path.exists(ssh_dir):
                import shutil
                if os.path.exists(docker_ssh_dir):
                    shutil.rmtree(docker_ssh_dir)
                shutil.copytree(ssh_dir, docker_ssh_dir)
            ssh_dir = docker_ssh_dir
        container_cmd = (
            f'set -e && '
            f'cp -r /root/.ssh_host /root/.ssh && '
            f'chmod 700 /root/.ssh && '
            f'chmod 600 /root/.ssh/* 2>/dev/null || true && '
            f'cat /root/.ssh/*.pub > /root/.ssh/authorized_keys 2>/dev/null && '
            f'chmod 600 /root/.ssh/authorized_keys 2>/dev/null || true && '
            f'echo "Host *" > /root/.ssh/config && '
            f'echo "    Port {ssh_port}" >> /root/.ssh/config && '
            f'echo "    StrictHostKeyChecking no" >> /root/.ssh/config && '
            f'chmod 600 /root/.ssh/config && '
            f'sed -i "s/^#*Port .*/Port {ssh_port}/" /etc/ssh/sshd_config && '
            f'/usr/sbin/sshd && '
            f'sleep infinity'
        )

        # Create compose configuration using the global container image.
        # Shared and private directories are mounted at the SAME path
        # inside the container so that all configuration paths (hostfile,
        # pipeline YAML, package data) are identical on host and container.
        private_dir = self.jarvis.get_pipeline_private_dir(self.name)

        # Prepare volume mounts — identical host:container paths
        # (use to_host_path for Docker-in-Docker remapping)
        volumes = [
            f"{to_host_path(str(private_dir))}:{private_dir}",
            f"{to_host_path(str(shared_dir))}:{shared_dir}",
            f"{to_host_path(ssh_dir)}:/root/.ssh_host:ro"
        ]


        service_config = {
            'container_name': container_name,
            'image': self.container_image,
            'entrypoint': ['/bin/bash', '-c'],
            'command': [container_cmd],
            'network_mode': 'host',
            'ipc': 'host',  # Share IPC namespace with host (removes shm limits)
            'volumes': volumes
        }

        # Inject container environment variables into the compose service
        if self.container_env:
            service_config['environment'] = {
                str(k): os.path.expandvars(str(v))
                for k, v in self.container_env.items()
            }

        # Note: GPU configuration is not included by default
        # If GPU access is needed, users should add it to their pipeline configuration
        # or use host network mode which provides direct device access

        # Apply container extensions from pipeline configuration
        if self.container_extensions:
            # Deep merge container_extensions into service_config
            self._merge_dict(service_config, self.container_extensions)

        compose_config = {
            'services': {
                self.name: service_config
            }
        }

        # Write compose file
        with open(compose_path, 'w') as f:
            yaml.dump(compose_config, f, default_flow_style=False)

        print(f"Generated docker-compose file: {compose_path}")
        return compose_path

    def _merge_dict(self, target: dict, source: dict):
        """
        Deep merge source dictionary into target dictionary.
        Lists are extended, dictionaries are recursively merged.

        :param target: Target dictionary to merge into
        :param source: Source dictionary to merge from
        """
        for key, value in source.items():
            if key in target:
                if isinstance(target[key], dict) and isinstance(value, dict):
                    # Recursively merge nested dictionaries
                    self._merge_dict(target[key], value)
                elif isinstance(target[key], list) and isinstance(value, list):
                    # Extend lists
                    target[key].extend(value)
                else:
                    # Override value
                    target[key] = value
            else:
                # Add new key
                target[key] = value

    def _start_containerized_pipeline(self):
        """
        Start containerized pipeline:
        1. Launch containers in detached mode (SSH daemons only)
        2. Run each package's start() — commands reach containers via SSH/MPI
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo

        logger.info("Starting containerized pipeline deployment")

        engine = self.container_engine.lower()

        if engine == 'apptainer':
            # Apptainer: start persistent instances with sshd on every node
            # (mirrors docker compose up -d).
            shared_dir = self.jarvis.get_pipeline_shared_dir(self.name)
            sif_path = shared_dir / f'{self.name}.sif'
            instance_name = self.name
            ssh_port = self.container_ssh_port

            _cg = getattr(self, 'container_gpu', False)
            if _cg is True or _cg == 'nvidia':
                nv_flag = '--nv '
            elif _cg == 'intel':
                # Intel GPU passthrough on Aurora-class nodes.
                #
                # IMPORTANT: do NOT add `--bind /dev/dri` or
                # `--bind /sys/class/drm`. Apptainer already auto-mounts
                # /dev (including /dev/dri) and /sys by default, with the
                # correct permissions for an unprivileged user namespace.
                # Explicitly binding /dev/dri replaces apptainer's
                # namespace-aware /dev mount with a host bind-mount that
                # the mapped-to-root user cannot open(O_RDWR), producing
                # EACCES on /dev/dri/renderD* even though the host nodes
                # are mode 666. See apptainer/apptainer#2963.
                #
                # With default auto-mounts, the container's Level Zero
                # runtime (installed in sycl/build.sh) enumerates all
                # 6 PVC tiles of an Aurora node correctly.
                #
                # We only forward PBS-allocated GPU affinity env vars so
                # SYCL sees only the tiles assigned to this job:
                #   ZE_AFFINITY_MASK     — which PVC tiles are visible
                #   ZEX_NUMBER_OF_CCS    — compute command streamer split
                import os as _os
                ze_envs = []
                for _k in ('ZE_AFFINITY_MASK', 'ZEX_NUMBER_OF_CCS',
                           'ZE_FLAT_DEVICE_HIERARCHY',
                           'ONEAPI_DEVICE_SELECTOR'):
                    _v = _os.environ.get(_k)
                    if _v:
                        ze_envs.append(f'--env {_k}={_v}')
                nv_flag = (' '.join(ze_envs) + ' ') if ze_envs else ''
            else:
                nv_flag = ''
            # Bind the pipeline shared dir so Lustre paths like
            # /lus/flare/... are visible inside the container (apptainer
            # only auto-binds /home, /tmp, /proc, /sys, /dev by default).
            bind_flag = f'--bind {shared_dir} '
            start_cmd = (
                f"apptainer instance start {nv_flag}{bind_flag}--writable-tmpfs {sif_path} {instance_name}"
                f" && apptainer exec {nv_flag}{bind_flag}instance://{instance_name}"
                f" /usr/sbin/sshd -p {ssh_port}"
                f" -o StrictModes=no -o UsePAM=no"
            )

            hostfile = self.get_hostfile()
            if self._hostfile_is_local_only(hostfile):
                logger.info("Hostfile is local-only, deploying to localhost directly")
                exec_info = LocalExecInfo()
            else:
                exec_info = PsshExecInfo(hostfile=hostfile)

            # Pre-create pipeline's private_dir on every host. Per-package
            # `apptainer exec` invocations (built by exec_factory) auto-bind
            # the package's private_dir into the container. Apptainer's
            # bind-mount requires the source path to exist on the executing
            # host. private_dir lives under /tmp/jarvis_private (machine-
            # local) and is created only on the deployer node by pkg.py's
            # _setup_directories, so remote hosts would otherwise fail with
            # "mount source ... doesn't exist". Also pre-create per-package
            # private subdirs for each package in the pipeline.
            private_dir = self.jarvis.get_pipeline_private_dir(self.name)
            mkdir_paths = [str(private_dir)]
            for _pkg_def in self.packages:
                _pkg_id = _pkg_def.get('pkg_id') or _pkg_def.get('pkg_name') or \
                          _pkg_def['pkg_type'].split('.')[-1]
                mkdir_paths.append(str(private_dir / _pkg_id))
            mkdir_cmd = 'mkdir -p ' + ' '.join(f'"{p}"' for p in mkdir_paths)
            Exec(mkdir_cmd, exec_info).run()

            Exec(start_cmd, exec_info).run()
            logger.success("Apptainer instances started (SSH ready)")
        else:
            # Docker/Podman: start containers via compose
            shared_dir = self.jarvis.get_pipeline_shared_dir(self.name)
            compose_path = shared_dir / 'docker-compose.yaml'

            if not compose_path.exists():
                raise FileNotFoundError(f"Compose file not found: {compose_path}. Did you load the pipeline?")

            if engine == 'podman':
                up_cmd = f"podman-compose -f {compose_path} up -d"
            else:
                up_cmd = f"docker compose -f {compose_path} up -d"

            hostfile = self.get_hostfile()
            if self._hostfile_is_local_only(hostfile):
                logger.info("Hostfile is local-only, deploying to localhost directly")
                exec_info = LocalExecInfo()
            else:
                logger.info("Deploying containers to all nodes in hostfile")
                self._distribute_image_to_hosts(hostfile)
                exec_info = PsshExecInfo(hostfile=hostfile)

            Exec(up_cmd, exec_info).run()
            logger.success("Containers started (SSH ready)")

        # Now run each package via the normal per-package flow.
        # Packages use PsshExecInfo / MpiExecInfo with the hostfile,
        # which reaches the containers through SSH on the configured port.
        for pkg_def in self.packages:
            try:
                logger.success(f"[{pkg_def['pkg_type']}] [START] BEGIN")
                pkg_instance = self._load_package_instance(pkg_def, self.env)
                self._apply_interceptors_to_package(pkg_instance, pkg_def)

                if hasattr(pkg_instance, 'start'):
                    pkg_instance.start()
                else:
                    logger.warning(f"Package {pkg_def['pkg_id']} has no start method")

                self.env.update(pkg_instance.env)
                logger.success(f"[{pkg_def['pkg_type']}] [START] END")
            except Exception as e:
                logger.error(f"Error starting package {pkg_def['pkg_id']}: {e}")
                raise RuntimeError(
                    f"Pipeline startup failed at package '{pkg_def['pkg_id']}': {e}"
                ) from e

    def _distribute_image_to_hosts(self, hostfile):
        """
        Ship the locally-built deploy image to every remote host's docker/podman
        daemon so `docker compose up -d` finds it without a registry pull.
        Mirrors the apptainer flow: save the image once to the pipeline's
        shared dir, then PSSH a `docker load -i ...` to every host that
        doesn't already have the image. Apptainer itself is skipped — its
        SIF is already on the shared FS.
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo
        from jarvis_cd.util.hostfile import Hostfile

        engine = self.container_engine.lower()
        if engine == 'apptainer':
            return

        image = self.name
        local_host = socket.gethostname()
        skip = ('localhost', '127.0.0.1', local_host)

        # Probe each remote host; only transfer to those missing the image
        needs_transfer = []
        for host in hostfile.hosts:
            if host in skip:
                continue
            check_cmd = (
                f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no "
                f"{host} '{engine} image inspect {image} >/dev/null 2>&1'"
            )
            if Exec(check_cmd, LocalExecInfo(hide_output=True)).run().exit_code.get('localhost', 1) == 0:
                logger.info(f"Image '{image}' already on {host}, skipping transfer")
            else:
                needs_transfer.append(host)

        if not needs_transfer:
            return

        # Save image once to the pipeline's shared dir (visible from every node)
        shared_dir = self.jarvis.get_pipeline_shared_dir(self.name)
        tar_path = shared_dir / f"{image}.tar"
        logger.info(f"Saving image '{image}' to {tar_path}...")
        save = Exec(f"{engine} save -o {tar_path} {image}", LocalExecInfo()).run()
        if save.exit_code.get('localhost', 1) != 0:
            raise RuntimeError(f"Failed to save image '{image}' to {tar_path}")

        # PSSH-load on every host that needs it (parallel)
        logger.info(f"Loading image '{image}' on {needs_transfer} in parallel...")
        load = Exec(
            f"{engine} load -i {tar_path}",
            PsshExecInfo(hostfile=Hostfile(hosts=needs_transfer))
        ).run()
        for host, code in load.exit_code.items():
            if code != 0:
                raise RuntimeError(f"Failed to load image '{image}' on {host}")

        # Tar served its purpose; reclaim the disk
        try:
            tar_path.unlink()
        except OSError:
            pass

    def _stop_containerized_pipeline(self):
        """
        Stop containerized pipeline:
        1. Stop each package via its stop() method
        2. Bring down the containers
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo

        logger.info("Stopping containerized pipeline")

        # Stop packages in reverse order (same as non-containerized)
        for pkg_def in reversed(self.packages):
            try:
                logger.success(f"[{pkg_def['pkg_type']}] [STOP] BEGIN")
                pkg_instance = self._load_package_instance(pkg_def, self.env)
                if hasattr(pkg_instance, 'stop'):
                    pkg_instance.stop()
                logger.success(f"[{pkg_def['pkg_type']}] [STOP] END")
            except Exception as e:
                logger.error(f"Error stopping package {pkg_def['pkg_id']}: {e}")

        # Bring down the containers
        engine = self.container_engine.lower()
        if engine == 'apptainer':
            stop_cmd = f"apptainer instance stop {self.name}"
        elif engine == 'podman':
            compose_path = self.jarvis.get_pipeline_shared_dir(self.name) / 'docker-compose.yaml'
            stop_cmd = f"podman-compose -f {compose_path} down"
        else:
            compose_path = self.jarvis.get_pipeline_shared_dir(self.name) / 'docker-compose.yaml'
            stop_cmd = f"docker compose -f {compose_path} down"

        hostfile = self.get_hostfile()
        if self._hostfile_is_local_only(hostfile):
            exec_info = LocalExecInfo()
        else:
            exec_info = PsshExecInfo(hostfile=hostfile)

        Exec(stop_cmd, exec_info).run()
        logger.success("Containers stopped")

    def _kill_containerized_pipeline(self):
        """
        Force-kill containerized pipeline:
        1. Kill each package
        2. Force-remove the containers
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo

        logger.info("Force-killing containerized pipeline")

        # Kill packages
        for pkg_def in self.packages:
            try:
                logger.success(f"[{pkg_def['pkg_type']}] [KILL] BEGIN")
                pkg_instance = self._load_package_instance(pkg_def, self.env)
                if hasattr(pkg_instance, 'kill'):
                    pkg_instance.kill()
                logger.success(f"[{pkg_def['pkg_type']}] [KILL] END")
            except Exception as e:
                logger.error(f"Error killing package {pkg_def['pkg_id']}: {e}")

        # Force-remove containers
        engine = self.container_engine.lower()
        if engine == 'apptainer':
            kill_cmd = f"apptainer instance stop {self.name}"
        elif engine == 'podman':
            compose_path = self.jarvis.get_pipeline_shared_dir(self.name) / 'docker-compose.yaml'
            kill_cmd = f"podman-compose -f {compose_path} kill && podman-compose -f {compose_path} down"
        else:
            compose_path = self.jarvis.get_pipeline_shared_dir(self.name) / 'docker-compose.yaml'
            kill_cmd = f"docker compose -f {compose_path} kill && docker compose -f {compose_path} down"

        hostfile = self.get_hostfile()
        if self._hostfile_is_local_only(hostfile):
            exec_info = LocalExecInfo()
        else:
            exec_info = PsshExecInfo(hostfile=hostfile)

        Exec(kill_cmd, exec_info).run()
        logger.success("Containers force-killed")