"""
AI Training — Distributed PyTorch training on GPUs.
Supports single-node multi-GPU and multi-node distributed training via torchrun.
"""
from jarvis_cd.core.route_pkg import RouteApp


class AiTraining(RouteApp):
    """
    Router class for AI Training deployment.
    """

    def _configure_menu(self):
        base_menu = super()._configure_menu()
        for item in base_menu:
            if item['name'] == 'deploy_mode':
                item['choices'] = ['default', 'container']
                break

        return base_menu + [
            {
                'name': 'script',
                'msg': 'Path to Python training script',
                'type': str,
                'default': '/opt/train_example.py',
            },
            {
                'name': 'epochs',
                'msg': 'Number of training epochs',
                'type': int,
                'default': 5,
            },
            {
                'name': 'batch',
                'msg': 'Batch size per GPU',
                'type': int,
                'default': 128,
            },
            {
                'name': 'nnodes',
                'msg': 'Number of nodes for distributed training',
                'type': int,
                'default': 1,
            },
            {
                'name': 'nproc_per_node',
                'msg': 'Number of GPUs per node',
                'type': int,
                'default': 1,
            },
            {
                'name': 'master_addr',
                'msg': 'Master node address for distributed training',
                'type': str,
                'default': 'localhost',
            },
            {
                'name': 'master_port',
                'msg': 'Master node port for distributed training',
                'type': int,
                'default': 29500,
            },
            {
                'name': 'out',
                'msg': 'Output directory for checkpoints and logs',
                'type': str,
                'default': '/tmp/ai_training_out',
            },
            {
                'name': 'base_image',
                'msg': 'Base Docker image for build container',
                'type': str,
                'default': 'sci-hpc-base',
            },
        ]
