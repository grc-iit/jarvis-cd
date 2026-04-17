"""
ADIOS2 — Adaptable I/O System for high-performance data transport.
"""
from jarvis_cd.core.pkg import Library


class Adios2(Library):
    """
    Builds and installs ADIOS2 from source.
    Requires HDF5 to be installed first if use_hdf5 is True.
    Must appear before packages that depend on it (wrf, xcompact3d, etc.).
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
                'name': 'base_image',
                'msg': 'Base image for build container',
                'type': str,
                'default': 'ubuntu:24.04',
            },
        ]

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        use_hdf5 = 'ON' if self.config.get('use_hdf5', True) else 'OFF'
        use_mpi = 'ON' if self.config.get('use_mpi', True) else 'OFF'
        content = self._read_build_script('build.sh', {
            'ADIOS2_VERSION': self.config.get('version', 'v2.11.0'),
            'ADIOS2_USE_HDF5': use_hdf5,
            'ADIOS2_USE_MPI': use_mpi,
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
