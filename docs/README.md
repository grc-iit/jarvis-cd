# Jarvis-CD Documentation

Jarvis-CD is a unified platform for deploying scientific applications, storage systems, and benchmarks through pipeline configuration files.

## Start Here

- **[Getting Started](getting_started.md)** — Install Jarvis, write your first pipeline YAML, run IOR

## Reference

- [Package Deployment Contracts](package-deployment.md) - Versioned execution, runtime resolution, configuration, and readiness metadata
- [Pipeline Configuration](pipelines.md) — Full YAML format, install managers (container/spack), environment management, multi-node, devcontainers
- [Package Development Guide](package_dev_guide.md) — Create new packages, Dockerfile templates, container and spack support
- [Pipeline Tests](pipeline_tests.md) — Automated testing with grid search and parameter sweeps
- [Execution Handles](executions.md) — Durable direct and scheduler run identities, status records, and JSON queries
- [Generated Artifacts](artifacts.md) — Durable output manifests, package providers, lifecycle, and location semantics
- [Service Runtimes](service-runtimes.md) — Durable network-service lifecycle, intrinsic dataset descriptors, and ParaView HTTP/SSE commands
- [Hostfile Configuration](hostfile.md) — Multi-node setup, pattern expansion, IP resolution
- [Scheduler Integration](scheduler.md) — Submit pipelines as SLURM jobs, automatic hostfile from `$SLURM_JOB_NODELIST`
- [Resource Graph](resource_graph.md) — Storage and network topology discovery
- [Modules](modules.md) — Manual package installation with environment tracking
- [Shell Execution](shell.md) — Exec, MPI, SSH, and SCP execution infrastructure
- [Argument Parsing](argparse.md) — CLI argument parser internals
