"""
VPIC-Kokkos — Vector Particle-In-Cell plasma physics simulation.
GPU-accelerated, relativistic, kinetic PIC code from Los Alamos National Lab.
"""
from jarvis_cd.core.route_pkg import RouteApp


class Vpic(RouteApp):
    """
    Router class for VPIC deployment.
    """

    def _configure_menu(self):
        base_menu = super()._configure_menu()
        for item in base_menu:
            if item['name'] == 'deploy_mode':
                item['choices'] = ['default', 'container']
                break

        return base_menu + [
            {
                'name': 'nprocs',
                'msg': 'Number of MPI processes',
                'type': int,
                'default': 4,
            },
            {
                'name': 'ppn',
                'msg': 'Processes per node',
                'type': int,
                'default': 4,
            },
            {
                'name': 'deck',
                'msg': 'Path to VPIC input deck (.cxx file)',
                'type': str,
                'default': None,
            },
            {
                'name': 'sample_deck',
                'msg': 'Built-in sample deck to use (harris, lpi, langmuir_wave)',
                'type': str,
                'choices': ['harris', 'lpi', 'langmuir_wave', 'custom'],
                'default': 'harris',
            },
            {
                'name': 'run_dir',
                'msg': 'Working directory for VPIC run',
                'type': str,
                'default': '/tmp/vpic_run',
            },
            {
                'name': 'cuda_arch',
                'msg': 'CUDA architecture code (80=A100, 90=H100, 70=V100)',
                'type': int,
                'default': 80,
            },
            {
                'name': 'base_image',
                'msg': 'Base Docker image for build container',
                'type': str,
                'default': 'sci-hpc-base',
            },
        ]
