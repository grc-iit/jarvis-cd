"""
WarpX — Exascale Particle-In-Cell plasma accelerator simulation.
Highly parallel, GPU-optimized PIC code built on AMReX.
"""
from jarvis_cd.core.route_pkg import RouteApp


class Warpx(RouteApp):
    """
    Router class for WarpX deployment.
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
                'default': 2,
            },
            {
                'name': 'ppn',
                'msg': 'Processes per node',
                'type': int,
                'default': 2,
            },
            {
                'name': 'inputs',
                'msg': 'Path to WarpX inputs file',
                'type': str,
                'default': None,
            },
            {
                'name': 'example',
                'msg': 'Built-in example to run (e.g., laser_acceleration, uniform_plasma)',
                'type': str,
                'choices': ['laser_acceleration', 'uniform_plasma', 'custom'],
                'default': 'laser_acceleration',
            },
            {
                'name': 'max_step',
                'msg': 'Total number of time steps',
                'type': int,
                'default': 50,
            },
            {
                'name': 'n_cell',
                'msg': 'Base grid cells as "nx ny nz"',
                'type': str,
                'default': '64 64 128',
            },
            {
                'name': 'out',
                'msg': 'Output directory for plot files',
                'type': str,
                'default': '/tmp/warpx_out',
            },
            {
                'name': 'plot_int',
                'msg': 'Plot output interval (-1 to disable)',
                'type': int,
                'default': 10,
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
