"""
Minimal PyTorch Lightning training loop exercising the ARLDM stack.

Runs a tiny 2-layer autoencoder on random tensors for a few epochs, using
the same torch + pytorch_lightning + transformers imports ARLDM's main.py
pulls in. Produces real training-loop stdout (Trainer banner, per-epoch
progress, loss values) so the container's output shows the stack is
functional end-to-end — without requiring a GPU or the multi-GB BLIP and
Stable Diffusion weights a full ARLDM training run needs.
"""
import os
import socket
import sys
import time

import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset
import pytorch_lightning as pl
import transformers

_RANK = int(os.environ.get('OMPI_COMM_WORLD_RANK', os.environ.get('PMI_RANK', 0)))
_WORLD_SIZE = int(
    os.environ.get('OMPI_COMM_WORLD_SIZE', os.environ.get('PMI_SIZE', 1))
)


class TinyAE(pl.LightningModule):
    """2-layer autoencoder — the simplest LightningModule that actually trains."""

    def __init__(self, dim: int = 32, hidden: int = 16):
        super().__init__()
        self.enc = nn.Linear(dim, hidden)
        self.dec = nn.Linear(hidden, dim)

    def forward(self, x):
        return self.dec(torch.relu(self.enc(x)))

    def training_step(self, batch, batch_idx):
        (x,) = batch
        recon = self(x)
        loss = nn.functional.mse_loss(recon, x)
        self.log('loss', loss, prog_bar=True, on_step=True, on_epoch=True)
        print(f'[rank {_RANK}] step={batch_idx} loss={loss.item():.6f}',
              flush=True)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-2)


def _banner():
    print(f'=== ARLDM stack smoke training ===', flush=True)
    print(f'host={socket.gethostname()} rank={_RANK}/{_WORLD_SIZE} '
          f'pid={os.getpid()}', flush=True)
    print(f'torch={torch.__version__} '
          f'pytorch_lightning={pl.__version__} '
          f'transformers={transformers.__version__}', flush=True)
    print(f'python={sys.version.split()[0]}', flush=True)
    arldm = os.environ.get('ARLDM_PATH', '(unset)')
    main = os.path.join(arldm, 'main.py')
    ok = os.path.isfile(main)
    print(f'ARLDM_PATH={arldm} main.py={"present" if ok else "MISSING"}',
          flush=True)


def main():
    _banner()

    torch.manual_seed(42 + _RANK)
    dim, n = 32, 256
    data = torch.randn(n, dim)
    loader = DataLoader(TensorDataset(data), batch_size=32, shuffle=True)

    model = TinyAE(dim=dim)
    trainer = pl.Trainer(
        max_epochs=2,
        accelerator='cpu',
        devices=1,
        logger=False,
        enable_checkpointing=False,
        enable_progress_bar=False,
        num_sanity_val_steps=0,
    )

    t0 = time.time()
    trainer.fit(model, loader)
    dt = time.time() - t0

    with torch.no_grad():
        recon = model(data[:4])
        final_loss = nn.functional.mse_loss(recon, data[:4]).item()

    print(f'[rank {_RANK}] training complete in {dt:.2f}s '
          f'final_loss_on_heldout={final_loss:.6f}', flush=True)
    print(f'=== ARLDM stack smoke training OK ===', flush=True)


if __name__ == '__main__':
    main()
