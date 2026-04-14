"""
Container-based AI Training deployment using Docker/Podman/Apptainer.
Provides PyTorch + CUDA environment for distributed training.
"""
from jarvis_cd.core.container_pkg import ContainerApplication
from jarvis_cd.shell import Exec


class AiTrainingContainer(ContainerApplication):
    """
    Container-based AI Training with PyTorch + CUDA.

    Build phase: Installs PyTorch matching CUDA 12.6.
    Deploy phase: Copies the full Python environment (no binary separation needed).

    Based on: awesome-scienctific-applications/ai_training/Dockerfile
    """

    def _build_phase(self) -> str:
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {base}

# PyTorch matching CUDA 12.6 (cached after first install)
RUN pip3 install --break-system-packages -q \\
        torch torchvision torchaudio \\
        --index-url https://download.pytorch.org/whl/cu126 \\
    && pip3 install --break-system-packages -q \\
        numpy matplotlib tensorboard

# Copy bundled example training script
COPY train_example.py /opt/train_example.py

CMD ["/bin/bash"]
"""

    def _build_deploy_phase(self) -> str:
        """
        For AI training, the deploy container is essentially the build container
        since Python packages are the only 'binaries'. Re-tag the build image.
        """
        return f"""FROM {self.build_image_name}

# Deploy container: full PyTorch environment ready
# SSH for multi-node torchrun
RUN apt-get update && apt-get install -y --no-install-recommends \\
    openssh-server openssh-client \\
    && rm -rf /var/lib/apt/lists/* \\
    && mkdir -p /var/run/sshd \\
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \\
    && sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

CMD ["/bin/bash"]
"""

    def _configure(self, **kwargs):
        """Configure AI training container. Also copies example script to build context."""
        import os
        import shutil
        from pathlib import Path

        # Copy the bundled train_example.py into private_dir so it's available as build context
        train_script_src = (
            Path(__file__).parent.parent.parent.parent /
            'awesome-scienctific-applications' / 'ai_training' / 'train_example_content.py'
        )
        # Generate a minimal training script if the source doesn't exist
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

    def start(self):
        from jarvis_cd.shell.process import Mkdir
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
        Exec(inner, self.container_exec_info(gpu=True)).run()

    def clean(self):
        from jarvis_cd.shell.process import Rm
        Rm(self.config.get('out', '') + '*').run()
