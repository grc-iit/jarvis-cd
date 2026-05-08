"""
Compression Libraries — FPZIP, SZ3, std_compat, and LibPressio.
"""
from jarvis_cd.core.pkg import Library


class CompressLibs(Library):
    """
    Builds and installs lossy/lossless compression libraries from source.
    Installs FPZIP, SZ3, std_compat, and LibPressio with ZFP/SZ3/FPZIP backends.
    """

    def _configure_menu(self):
        return [
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
        content = self._read_build_script('build.sh', {})
        return content, 'default'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        suffix = getattr(self, '_build_suffix', '')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, suffix
