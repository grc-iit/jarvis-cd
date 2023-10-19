#!/bin/bash
cd ##dataset_location##
wget -c https://www2.mmm.ucar.edu/wrf/users/benchmark/v44/v4.4_bench_conus12km.tar.gz
tar -xvzf v4.4_bench_conus12km.tar.gz
cd v4.4_bench_conus12km/
cp wrfbdy_d01 ##wrf_location##
cp wrfinput_d01 ##wrf_location##
