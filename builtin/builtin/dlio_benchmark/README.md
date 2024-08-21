DLIO is an I/O benchmark for Deep Learning, aiming at emulating the I/O behavior of various deep learning applications.

# Installation

```bash
git clone https://github.com/argonne-lcf/dlio_benchmark
cd dlio_benchmark/
pip install .
```

# DLIO

## 1. Create a Resource Graph

If you haven't already, create a resource graph. This only needs to be done
once throughout the lifetime of Jarvis. No need to repeat if you have already
done this for a different pipeline.

If you are running distributed tests, set path to the hostfile you are  using.
```bash
jarvis hostfile set /path/to/hostfile
```

Next, collect the resources from each of those pkgs. Walkthrough will give
a command line tutorial on how to build the hostfile.
```bash
jarvis resource-graph build +walkthrough
```

## 2. Create a Pipeline

The Jarvis pipeline will store all configuration data.
```bash
jarvis pipeline create dlio_test
```

## 4. Add pkgs to the Pipeline

Create a Jarvis pipeline
```bash
jarvis pipeline append dlio_benchmark workload=unet3d_a100 generate_data=True data_path=/path/to/generated_data checkpoint_path=/path/to/checkpoints
```
Note: you can modify the dlio_benchmark configuration file by changing the modifying the dlio_benchmark.yaml file directly, and then execute `jarvis ppl update` to update the configuration. 

## 5. Run Experiment

Run the experiment
```bash
jarvis pipeline run
```

## 6. Clean Data

Clean produced data
```bash
jarvis pipeline clean
```