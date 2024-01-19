Gray-Scott is a 3D 7-Point stencil code

# Installation

Check the README for gadget2.

# Create Pipeline (Gassphere)

```bash
jarvis pipeline create ngenic
jarvis pipeline env copy gadget2
jarvis pipeline append gadget2_df
jarvis pkg configure gadget2_df \
tile_fac=1 \
nprocs=4
jarvis pipeline run
```