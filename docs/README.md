# Jarvis-CD Documentation

Jarvis-CD is a unified platform for deploying scientific applications, storage systems, and benchmarks through pipeline configuration files.

## Start Here

- **[Getting Started](getting_started.md)** — Install Jarvis, write your first pipeline YAML, run IOR

## Reference

- [Pipeline Configuration](pipelines.md) — Full YAML format, install managers (container/spack), environment management, multi-node, devcontainers
- [Package Development Guide](package_dev_guide.md) — Create new packages, Dockerfile templates, container and spack support
- [Pipeline Tests](pipeline_tests.md) — Automated testing with grid search and parameter sweeps
- [Hostfile Configuration](hostfile.md) — Multi-node setup, pattern expansion, IP resolution
- [Resource Graph](resource_graph.md) — Storage and network topology discovery
- [Modules](modules.md) — Manual package installation with environment tracking
- [Shell Execution](shell.md) — Exec, MPI, SSH, and SCP execution infrastructure
- [Argument Parsing](argparse.md) — CLI argument parser internals
