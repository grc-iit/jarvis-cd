"""
Nyx — AMReX-based cosmological simulation (HydroTests).
Adaptive mesh, massively parallel simulation code from AMReX-Astro.
"""
from jarvis_cd.core.route_pkg import RouteApp


class Nyx(RouteApp):
    """
    Router class for Nyx cosmological simulation deployment.
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
                'name': 'max_step',
                'msg': 'Number of coarse time steps',
                'type': int,
                'default': 100,
            },
            {
                'name': 'n_cell',
                'msg': 'Base grid cells as "nx ny nz"',
                'type': str,
                'default': '128 128 128',
            },
            {
                'name': 'max_level',
                'msg': 'Maximum AMR refinement level',
                'type': int,
                'default': 0,
            },
            {
                'name': 'out',
                'msg': 'Output directory for plot files',
                'type': str,
                'default': '/tmp/nyx_out',
            },
            {
                'name': 'plot_int',
                'msg': 'Plot file interval (-1 to disable)',
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
