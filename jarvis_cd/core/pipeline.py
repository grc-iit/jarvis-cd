"""
Pipeline management for Jarvis-CD.
Provides the consolidated Pipeline class that combines pipeline creation, loading, and execution.
"""

import os
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

        # Add hostfile parameter (save path if set, None means use global jarvis hostfile)
        # For containerized pipelines, use the container-mounted path
        if self.hostfile:
            if self.container_image:
                # In container, hostfile will be mounted at /root/.ppi-jarvis/hostfile
                pipeline_config['hostfile'] = "/root/.ppi-jarvis/hostfile"
            else:
                # On host, use actual path
                pipeline_config['hostfile'] = self.hostfile.path if self.hostfile.path else ""
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

            # Configure all packages before starting
            # This runs _configure() on each package, which sets up
            # environment variables (e.g., CHI_SERVER_CONF) needed by start()
            # Skip when containerized — configure happens inside the container
            if not self.is_containerized():
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

        # Load hostfile parameter (None means use global jarvis hostfile)
        hostfile_path = pipeline_config.get('hostfile')
        if hostfile_path:
            self.hostfile = Hostfile(path=hostfile_path)
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

        # Load hostfile parameter (None means use global jarvis hostfile)
        hostfile_path = pipeline_def.get('hostfile')
        if hostfile_path:
            self.hostfile = Hostfile(path=hostfile_path)
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

        # Auto-build containers if any package has deploy_mode: container
        if not self.container_image and self._has_containerized_packages():
            self._build_pipeline_container()
            self.container_image = self.name

        # Save pipeline configuration and environment
        self.save()

        # Generate container compose file if this is a containerized pipeline
        if self.is_containerized():
            print(f"Generating container configuration files...")
            self._generate_pipeline_container_yaml()
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
        Build pipeline container images by collecting Dockerfiles from all packages.

        1. Instantiates each package (without configuring) to call _build_phase()/_build_deploy_phase()
        2. Builds per-package build images as jarvis-build-{pkg_name}
        3. Concatenates all deploy-phase Dockerfiles, builds as {pipeline_name}
        """
        from jarvis_cd.shell import Exec, LocalExecInfo

        deploy_image_name = self.name
        print(f"Building pipeline container: {deploy_image_name}")

        deploy_dockerfile_parts = []

        # Determine build engine
        pipeline_shared_dir = self.jarvis.get_pipeline_shared_dir(self.name)
        pipeline_shared_dir.mkdir(parents=True, exist_ok=True)
        build_engine = self.container_engine
        if build_engine == 'apptainer':
            import shutil
            build_engine = 'docker' if shutil.which('docker') else 'podman'

        # Collect Dockerfile content from all packages
        for pkg_def in self.packages:
            pkg_instance = self._load_package_instance(pkg_def, self.env)
            # Set deploy_mode in config so _build_phase/_build_deploy_phase return content
            pkg_instance.config['deploy_mode'] = pkg_def.get('config', {}).get('deploy_mode', 'default')

            # Build per-package build image
            build_content = pkg_instance._build_phase()
            if build_content:
                pkg_name = pkg_def['pkg_name']
                build_image_name = f"jarvis-build-{pkg_name}"
                build_dockerfile_path = pipeline_shared_dir / f'build-{pkg_name}.Dockerfile'
                with open(build_dockerfile_path, 'w') as f:
                    f.write(f"# --- {pkg_def['pkg_id']} build phase ---\n")
                    f.write(build_content)

                print(f"Building build image: {build_image_name}")
                build_cmd = (
                    f"{build_engine} build --network=host -t {build_image_name} "
                    f"-f {build_dockerfile_path} {pipeline_shared_dir}"
                )
                result = Exec(build_cmd, LocalExecInfo()).run()
                exit_code = result.exit_code.get('localhost', 1)
                if exit_code != 0:
                    raise RuntimeError(
                        f"Failed to build image '{build_image_name}' (exit code {exit_code}). "
                        f"Dockerfile: {build_dockerfile_path}"
                    )
                print(f"Build image ready: {build_image_name}")

            deploy_content = pkg_instance._build_deploy_phase()
            if deploy_content:
                deploy_dockerfile_parts.append(f"# --- {pkg_def['pkg_id']} deploy phase ---")
                deploy_dockerfile_parts.append(deploy_content)

        # Build the DEPLOY image (named after the pipeline)
        if deploy_dockerfile_parts:
            deploy_dockerfile = "\n".join(deploy_dockerfile_parts)
            deploy_dockerfile_path = pipeline_shared_dir / 'deploy.Dockerfile'
            with open(deploy_dockerfile_path, 'w') as f:
                f.write(deploy_dockerfile)

            print(f"Building deploy image: {deploy_image_name}")
            deploy_cmd = (
                f"{build_engine} build --network=host -t {deploy_image_name} "
                f"-f {deploy_dockerfile_path} {pipeline_shared_dir}"
            )
            result = Exec(deploy_cmd, LocalExecInfo()).run()
            exit_code = result.exit_code.get('localhost', 1)
            if exit_code != 0:
                raise RuntimeError(
                    f"Failed to build image '{deploy_image_name}' (exit code {exit_code}). "
                    f"Dockerfile: {deploy_dockerfile_path}"
                )
            print(f"Deploy image ready: {deploy_image_name}")
        else:
            print("Warning: No deploy Dockerfile content from any package")

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

        # Build container command - start SSH and run pipeline, then stop SSH
        ssh_dir = os.path.expanduser('~/.ssh')
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
            f'jarvis ppl run yaml /root/.ppi-jarvis/shared/pipeline.yaml; '
            f'EXIT_CODE=$?; '
            f'pkill sshd; '
            f'exit $EXIT_CODE'
        )

        # Create compose configuration using the global container image
        private_dir = self.jarvis.get_pipeline_private_dir(self.name)

        # Prepare volume mounts
        volumes = [
            f"{private_dir}:/root/.ppi-jarvis/private",
            f"{shared_dir}:/root/.ppi-jarvis/shared",
            f"{ssh_dir}:/root/.ssh_host:ro"
        ]

        # Add hostfile volume mount if hostfile is set
        hostfile = self.get_hostfile()
        if hostfile and hostfile.path:
            volumes.append(f"{hostfile.path}:/root/.ppi-jarvis/hostfile:ro")

        service_config = {
            'container_name': container_name,
            'image': self.container_image,
            'entrypoint': ['/bin/bash', '-c'],
            'command': [container_cmd],
            'network_mode': 'host',
            'ipc': 'host',  # Share IPC namespace with host (removes shm limits)
            'volumes': volumes
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
        Start containerized pipeline by deploying containers to all nodes in hostfile using pssh.
        Uses the pre-built global container image.
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import LocalExecInfo, PsshExecInfo
        from jarvis_cd.shell.container_compose_exec import ContainerComposeExec

        logger.info("Starting containerized pipeline deployment")

        # Get compose file path (already generated during load)
        shared_dir = self.jarvis.get_pipeline_shared_dir(self.name)
        compose_path = shared_dir / 'docker-compose.yaml'

        if not compose_path.exists():
            raise FileNotFoundError(f"Compose file not found: {compose_path}. Did you load the pipeline?")

        # Determine container runtime preference
        prefer_podman = self.container_engine.lower() == 'podman'

        # Check if we have a hostfile
        hostfile = self.get_hostfile()
        if not hostfile or len(hostfile) == 0:
            logger.warning("No hostfile found, deploying to localhost only")
            exec_info = LocalExecInfo()
        else:
            logger.info(f"Deploying containers to all nodes in hostfile")
            exec_info = PsshExecInfo(hostfile=hostfile)

        # Start containers (uses pre-built image)
        ContainerComposeExec(str(compose_path), exec_info, action='up', prefer_podman=prefer_podman).run()

        logger.success(f"Containers started")

    def _stop_containerized_pipeline(self):
        """
        Stop containerized pipeline by stopping containers on all nodes in hostfile using pssh.
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import LocalExecInfo, PsshExecInfo
        from jarvis_cd.shell.container_compose_exec import ContainerComposeExec

        logger.info("Stopping containerized pipeline")

        # Determine container runtime preference
        prefer_podman = self.container_engine.lower() == 'podman'

        # Get compose file path
        shared_dir = self.jarvis.get_pipeline_shared_dir(self.name)
        compose_path = shared_dir / 'docker-compose.yaml'

        # Check if we have a hostfile
        hostfile = self.get_hostfile()
        if not hostfile or len(hostfile) == 0:
            logger.warning("No hostfile found, stopping on localhost only")
            exec_info = LocalExecInfo()
        else:
            logger.info(f"Stopping containers on all nodes in hostfile")
            exec_info = PsshExecInfo(hostfile=hostfile)

        # Stop containers
        ContainerComposeExec(str(compose_path), exec_info, action='down', prefer_podman=prefer_podman).run()

        logger.success(f"Containers stopped")

    def _kill_containerized_pipeline(self):
        """
        Kill containerized pipeline by force-stopping containers on all nodes in hostfile using pssh.
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import LocalExecInfo, PsshExecInfo
        from jarvis_cd.shell.container_compose_exec import ContainerComposeExec

        logger.info("Force-killing containerized pipeline")

        # Determine container runtime preference
        prefer_podman = self.container_engine.lower() == 'podman'

        # Get compose file path
        shared_dir = self.jarvis.get_pipeline_shared_dir(self.name)
        compose_path = shared_dir / 'docker-compose.yaml'

        # Check if we have a hostfile
        hostfile = self.get_hostfile()
        if not hostfile or len(hostfile) == 0:
            logger.warning("No hostfile found, force-killing on localhost only")
            exec_info = LocalExecInfo()
        else:
            logger.info(f"Force-killing containers on all nodes in hostfile")
            exec_info = PsshExecInfo(hostfile=hostfile)

        # Kill and then remove containers
        ContainerComposeExec(str(compose_path), exec_info, action='kill', prefer_podman=prefer_podman).run()
        ContainerComposeExec(str(compose_path), exec_info, action='down', prefer_podman=prefer_podman).run()

        logger.success(f"Containers force-killed")