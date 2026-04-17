"""
HDF5 — Parallel HDF5 library for high-performance I/O.
"""
from jarvis_cd.core.pkg import Library


class Hdf5(Library):
    """
    Builds and installs HDF5 from source.
    Must appear before packages that depend on it (lammps, nyx, warpx, etc.).
    """

    def _configure_menu(self):
        return [
            {
                'name': 'version',
                'msg': 'HDF5 version to build',
                'type': str,
                'default': '2.1.1',
            },
            {
                'name': 'parallel',
                'msg': 'Enable parallel HDF5 (MPI)',
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
        content = self._read_build_script('build.sh', {
            'HDF5_VERSION': self.config.get('version', '2.1.1'),
        })
        return content, self.config.get('version', '2.1.1')

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        suffix = getattr(self, '_build_suffix', '')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, suffix
