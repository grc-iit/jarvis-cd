"""
This module provides classes and methods to launch the Gray-Scott application.
Gray-Scott is a reaction-diffusion simulation.
"""
from jarvis_cd.core.route_pkg import RouteApp


class GrayScott(RouteApp):
    """
    Router class for Gray-Scott deployment — delegates to default or container implementation.
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
                'default': None,
            },
            {
                'name': 'width',
                'msg': 'Global grid width (columns)',
                'type': int,
                'default': 512,
            },
            {
                'name': 'height',
                'msg': 'Global grid height (rows)',
                'type': int,
                'default': 512,
            },
            {
                'name': 'steps',
                'msg': 'Total number of time steps',
                'type': int,
                'default': 5000,
            },
            {
                'name': 'out_every',
                'msg': 'HDF5 output interval (steps between writes)',
                'type': int,
                'default': 500,
            },
            {
                'name': 'outdir',
                'msg': 'Output directory for HDF5 files',
                'type': str,
                'default': '/tmp/gray_scott_out',
            },
            {
                'name': 'F',
                'msg': 'Feed rate',
                'type': float,
                'default': 0.035,
            },
            {
                'name': 'k',
                'msg': 'Kill rate',
                'type': float,
                'default': 0.060,
            },
            {
                'name': 'Du',
                'msg': 'Diffusion coefficient for u',
                'type': float,
                'default': 0.16,
            },
            {
                'name': 'Dv',
                'msg': 'Diffusion coefficient for v',
                'type': float,
                'default': 0.08,
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
