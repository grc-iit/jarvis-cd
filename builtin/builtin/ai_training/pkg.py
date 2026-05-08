"""
AI Training — Distributed PyTorch training on GPUs.
Supports single-node multi-GPU and multi-node distributed training via torchrun.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm
from pathlib import Path


class AiTraining(Application):
    """
    Merged AiTraining class supporting both default (bare-metal) and container deployment.

    Set deploy_mode='container' to build and run the PyTorch training environment inside
    a Docker/Podman/Apptainer container.  Set deploy_mode='default' to use a
    system-installed Python/torchrun.
    """

    def _init(self):
        pass

    def _configure_menu(self):
        return [
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

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_build_script('build.sh', {
            'BASE_IMAGE': self.config.get('base_image', 'sci-hpc-base'),
        })
        return content, 'pytorch-cu126'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        suffix = getattr(self, '_build_suffix', '')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'nvidia/cuda:12.6.0-runtime-ubuntu24.04',
        })
        return content, suffix

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure AI Training.

        In container mode, copies (or generates) the bundled train_example.py into
        private_dir so it is available as Docker build context, then calls
        super()._configure() which updates self.config and triggers
        build_phase / build_deploy_phase.

        In default mode, calls super()._configure() and creates the output
        directory on all nodes.
        """
        if self.config.get('deploy_mode') == 'container' or kwargs.get('deploy_mode') == 'container':
            # Copy the bundled train_example.py into private_dir so it's
            # available as build context before the Docker build runs.
            private_path = Path(self.private_dir)
            private_path.mkdir(parents=True, exist_ok=True)
            train_script_dst = private_path / 'train_example.py'

            if not train_script_dst.exists():
                # Write a minimal placeholder
                with open(train_script_dst, 'w') as f:
                    f.write("""#!/usr/bin/env python3
\"\"\"Distributed data-parallel training example.\"\"\"
import os, argparse
import torch
import torch.nn as nn

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=3)
    parser.add_argument('--batch', type=int, default=128)
    args = parser.parse_args()

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    rank = int(os.environ.get('RANK', 0))
    if rank == 0:
        print(f"Training on {device}")

    for epoch in range(args.epochs):
        if rank == 0:
            print(f"Epoch {epoch+1}/{args.epochs}")

    if rank == 0:
        print("Training complete.")

if __name__ == '__main__':
    main()
""")

        super()._configure(**kwargs)

        if self.config.get('deploy_mode') == 'default':
            Mkdir(self.config['out'],
                  PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Launch AI Training.

        Branches on deploy_mode: uses LocalExecInfo with container engine for
        container mode, system torchrun via LocalExecInfo for default mode.
        """
        if self.config.get('deploy_mode') == 'container':
            Mkdir(self.config['out']).run()

            nnodes = self.config.get('nnodes', 1)
            nproc = self.config.get('nproc_per_node', 1)
            inner = ' '.join([
                'torchrun',
                f'--nnodes={nnodes}',
                f'--nproc_per_node={nproc}',
                '--node_rank=0',
                f'--master_addr={self.config["master_addr"]}',
                f'--master_port={self.config["master_port"]}',
                self.config['script'],
                f'--epochs {self.config["epochs"]}',
                f'--batch {self.config["batch"]}',
            ])
            Exec(inner, LocalExecInfo(
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                gpu=True,
                env=self.mod_env,
            )).run()
        else:
            node_rank = 0  # Single-node or head node
            cmd = [
                'torchrun',
                f'--nnodes={self.config["nnodes"]}',
                f'--nproc_per_node={self.config["nproc_per_node"]}',
                f'--node_rank={node_rank}',
                f'--master_addr={self.config["master_addr"]}',
                f'--master_port={self.config["master_port"]}',
                self.config['script'],
                f'--epochs {self.config["epochs"]}',
                f'--batch {self.config["batch"]}',
            ]
            Exec(' '.join(cmd), LocalExecInfo(env=self.mod_env)).run()

    def stop(self):
        """Stop AI Training (no-op — training runs to completion)."""
        pass

    def clean(self):
        """Remove AI Training output directory."""
        Rm(self.config['out'] + '*',
           PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()
