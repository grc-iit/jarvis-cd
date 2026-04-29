"""
ADIOS2 — Adaptable I/O System for high-performance data transport.
"""
from jarvis_cd.core.pkg import Library


class Adios2(Library):
    """
    Builds and installs ADIOS2 from source into /usr/local.

    Because this is a ``Library`` package, its ``/usr/local`` artifacts
    are injected into the build container before downstream package
    builds run, so every consuming package (wrf, xcompact3d, openfoam,
    gray_scott_paraview, …) can link against the same ADIOS2 install
    without re-building it. Must appear before those packages in the
    pipeline YAML.

    Pipeline knobs:
      version       — upstream git tag (e.g. v2.11.0, v2.10.2)
      use_hdf5      — build with HDF5 support. Off for apps that only
                      use the native BP engines (e.g. xcompact3d).
      use_mpi       — build with MPI support (almost always on).
      use_fortran   — build Fortran bindings (required by xcompact3d,
                      wrf custom couplers).
      use_python    — build Python bindings (required by
                      gray_scott_paraview / pvpython readers).
    """

    def _configure_menu(self):
        return [
            {
                'name': 'version',
                'msg': 'ADIOS2 version (git tag)',
                'type': str,
                'default': 'v2.11.0',
            },
            {
                'name': 'use_hdf5',
                'msg': 'Build with HDF5 support',
                'type': bool,
                'default': True,
            },
            {
                'name': 'use_mpi',
                'msg': 'Build with MPI support',
                'type': bool,
                'default': True,
            },
            {
                'name': 'use_fortran',
                'msg': 'Build ADIOS2 Fortran bindings',
                'type': bool,
                'default': False,
            },
            {
                'name': 'use_python',
                'msg': 'Build ADIOS2 Python bindings',
                'type': bool,
                'default': False,
            },
            {
                'name': 'base_image',
                'msg': 'Base image for build container',
                'type': str,
                'default': 'ubuntu:24.04',
            },
        ]

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None

        def on_off(key, default):
            return 'ON' if self.config.get(key, default) else 'OFF'

        content = self._read_build_script('build.sh', {
            'ADIOS2_VERSION':     self.config.get('version', 'v2.11.0'),
            'ADIOS2_USE_HDF5':    on_off('use_hdf5', True),
            'ADIOS2_USE_MPI':     on_off('use_mpi', True),
            'ADIOS2_USE_Fortran': on_off('use_fortran', False),
            'ADIOS2_USE_Python':  on_off('use_python', False),
        })
        return content, self.config.get('version', 'v2.11.0')

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        suffix = getattr(self, '_build_suffix', '')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, suffix
