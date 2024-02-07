Darshan is an I/O profiling tool. This repo intercepts application I/O and
dumps them into a log that can be parsed to collect metrics such as I/O time.

# Dependencies

# Compiling / Installing

```bash
scspkg create darshan
cd $(scspkg pkg src darshan)
git clone https://github.com/darshan-hpc/darshan.git
cd darshan
git fetch --all --tags --prune
git checkout tags/darshan-3.4.4
./prepare.sh

cd darshan-runtime
./configure --with-log-path=/darshan-logs \
--with-jobid-env=PBS_JOBID \
--with-log-path-by-env=DARSHAN_LOG_DIR \
--prefix=$(scspkg pkg root darshan) \
--enable-hdf5-mod \
CC=mpicc
# --enable-pnetcdf-mod \
make -j32
make install

cd ../darshan-util
./configure \
--prefix=$(scspkg pkg root darshan) \
--enable-pydarshan
make -j32
make install
```

# Usage

Create darshan environment:
```bash
module load darshan
jarvis env build darshan
```

Create a pipeline:
```bash
jarvis pipeline append darshan log_dir=${HOME}/darshan_logs
jarvis pipeline append ior
```

Run the pipeline:
```bash
jarvis pipeline run
```

# Analysis

There are several ways to analyze the output of Darshan:
```
darshan-job-summary.pl ${HOME}/darshan_logs
```