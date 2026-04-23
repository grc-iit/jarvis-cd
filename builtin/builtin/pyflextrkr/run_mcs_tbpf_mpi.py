#!/usr/bin/env python
"""Dask-MPI variant of upstream runscripts/run_mcs_tbpf.py.

Launch with:
    mpirun -n N python run_mcs_tbpf_mpi.py <config.yml>

Requires at least 3 MPI ranks (rank 0 scheduler, rank 1 client, rest
workers). The upstream runscript drives `Client(scheduler_file=..., n_workers)`
which requires launching the scheduler/workers out-of-band — we use
`dask_mpi.initialize()` instead so one `mpirun` handles the whole cluster.

This mirrors the pattern used in the Ares-branch of PyFLEXTRKR
(dayu_evaluations/runscripts/run_mcs_tb_summer_sam.py) that was
validated multi-node on bare-metal Slurm.

The xarray monkey-patch is required to keep the netCDF4 file_manager
LRU cache from deadlocking when many Dask workers open cloudid files
concurrently over a shared filesystem. `h5netcdf` is used for the HDF5
format cloudid files (no global lock); classic NetCDF3 inputs
(e.g. landmask files) fall back to the `netcdf4` engine.
"""
import os
import sys
import logging

import dask
import xarray as xr
from dask.distributed import Client, LocalCluster
from dask_mpi import initialize

from pyflextrkr.ft_utilities import load_config, setup_logging
from pyflextrkr.idfeature_driver import idfeature_driver
from pyflextrkr.tracksingle_driver import tracksingle_driver
from pyflextrkr.gettracks import gettracknumbers
from pyflextrkr.trackstats_driver import trackstats_driver
from pyflextrkr.identifymcs import identifymcs_tb
from pyflextrkr.matchtbpf_driver import match_tbpf_tracks
from pyflextrkr.robustmcspf import define_robust_mcs_pf
from pyflextrkr.mapfeature_driver import mapfeature_driver
from pyflextrkr.movement_speed import movement_speed


def _install_h5netcdf_patch():
    """Swap xarray's default netCDF4 engine for h5netcdf + disable
    file-manager caching. Applied on both the client and every worker."""
    import xarray as xr
    xr.set_options(file_cache_maxsize=1)
    _orig_open = xr.open_dataset
    _orig_open_mf = xr.open_mfdataset

    def _open_dataset_h5(*args, **kwargs):
        if "engine" not in kwargs:
            try:
                return _orig_open(*args, engine="h5netcdf", **kwargs)
            except (OSError, ValueError):
                return _orig_open(*args, engine="netcdf4", **kwargs)
        return _orig_open(*args, **kwargs)

    def _open_mfdataset_h5(*args, **kwargs):
        if "engine" not in kwargs:
            try:
                return _orig_open_mf(*args, engine="h5netcdf", **kwargs)
            except (OSError, ValueError):
                return _orig_open_mf(*args, engine="netcdf4", **kwargs)
        return _orig_open_mf(*args, **kwargs)

    xr.open_dataset = _open_dataset_h5
    xr.open_mfdataset = _open_mfdataset_h5


_install_h5netcdf_patch()


if __name__ == "__main__":

    setup_logging()
    logger = logging.getLogger(__name__)

    config_file = sys.argv[1]
    config = load_config(config_file)

    trackstats_filebase = config["trackstats_filebase"]
    mcstbstats_filebase = config["mcstbstats_filebase"]
    mcsrobust_filebase = config["mcsrobust_filebase"]

    run_parallel = config.get("run_parallel", 0)
    if run_parallel == 1:
        dask_tmp_dir = config.get("dask_tmp_dir", "./")
        dask.config.set({"temporary-directory": dask_tmp_dir})
        cluster = LocalCluster(n_workers=config["nprocesses"], threads_per_worker=1)
        client = Client(cluster)
        client.run(setup_logging)
    elif run_parallel == 2:
        dask_local_dir = config.get("dask_tmp_dir", "/tmp/pyflextrkr_dask")
        os.makedirs(dask_local_dir, exist_ok=True)
        initialize(dashboard=False, local_directory=dask_local_dir)
        client = Client()
        client.run(setup_logging)
        client.run(_install_h5netcdf_patch)
        logger.info(f"Dask-MPI client scheduler: {client.scheduler}")
    else:
        logger.info("Running in serial.")

    if config.get("run_idfeature", False):
        idfeature_driver(config)

    if config.get("run_tracksingle", False):
        tracksingle_driver(config)

    if config.get("run_gettracks", False):
        gettracknumbers(config)

    if config.get("run_trackstats", False):
        trackstats_driver(config)

    if config.get("run_identifymcs", False):
        identifymcs_tb(config)

    if config.get("run_matchpf", False):
        match_tbpf_tracks(config)

    if config.get("run_robustmcs", False):
        define_robust_mcs_pf(config)

    if config.get("run_mapfeature", False):
        mapfeature_driver(config, trackstats_filebase=mcsrobust_filebase)

    if config.get("run_speed", False):
        movement_speed(config, trackstats_filebase=mcsrobust_filebase)
