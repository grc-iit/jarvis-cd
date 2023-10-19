#!/bin/bash
cd ##dataset_location##
wget -c ##download_url##
tar -xvzf v4.4_bench_conus12km.tar.gz
cd v4.4_bench_conus12km/
cp wrfbdy_d01 ##wrf_location##
cp wrfinput_d01 ##wrf_location##
