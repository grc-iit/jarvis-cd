---
name: jarvis-package-dev
description: Use when developing, modifying, or reviewing Jarvis-CD packages (pkg.py files). Covers package scaffolding, required methods (_init, _configure_menu, _configure, start, stop, clean), base classes (Application, Service, Interceptor, Library), container support via build.sh + Dockerfile.deploy, environment propagation (self.env vs self.mod_env), and execution helpers (Exec, MpiExecInfo, PsshExecInfo). Trigger phrases: "jarvis package", "jarvis pkg", "builtin/<name>/pkg.py", "add container support", "write a pkg.py", "_build_phase", "_configure_menu", "Jarvis Application class", "Jarvis Interceptor", "modify_env".
version: 1.0.0
---

# Jarvis-CD Package Development

You are helping a developer write, modify, or review a Jarvis-CD package. Jarvis-CD is a pipeline management system where each "package" is a Python class that wraps an application, service, or interceptor, and is driven by `jarvis ppl` commands.

The authoritative reference is `docs/package_dev_guide.md`. This skill condenses the rules and patterns you must follow. When in doubt about edge cases, read the guide directly.

---

## 1. Repository and File Layout

A Jarvis-CD package repo must contain a subdirectory matching the repo name and a `pipelines/` index directory:

```
my_repo/
├── my_repo/
│   ├── __init__.py
│   ├── my_package/
│   │   ├── __init__.py
│   │   ├── pkg.py              # REQUIRED — single class extending Application/Service/Interceptor/Library
│   │   ├── build.sh            # Optional — build script for container mode
│   │   ├── Dockerfile.deploy   # Optional — deploy template for container mode
│   │   ├── config/             # Optional — template config files (read via self.pkg_dir)
│   │   └── README.md           # Optional
│   └── …
├── pipelines/                  # YAML pipeline examples, discoverable via `jarvis ppl index`
└── README.md
```

Key rules:
- File name **must** be `pkg.py`, not `package.py`.
- Class name **must** be UpperCamelCase of the directory name: `ior` → `Ior`, `data_stagein` → `DataStagein`, `gray_scott_paraview` → `GrayScottParaview`. Mismatch causes a fatal load error.
- `jarvis repo add /path/to/my_repo` registers the repo.

## 2. Base Classes — Pick the Right One

All packages ultimately inherit from `Pkg`. Choose the most specific base class:

| Base class | Use for | Key hooks |
|---|---|---|
| `Application` | Jobs that run and complete (benchmarks, sims, data-processing apps) | `start`, `stop` (usually no-op), `clean` |
| `Service` | Long-running daemons (databases, pub/sub brokers) | `start`, `stop`, `kill`, `status` |
| `Interceptor` | Packages that modify another package's env (LD_PRELOAD, profilers) | `modify_env` (NOT `start`) |
| `Library` | Build-only packages that supply artifacts to later packages (hdf5, adios2) | `_build_phase`, `_build_deploy_phase`; skipped from final pipeline merge |
| `SimplePackage` (legacy) | Packages needing per-package interceptor list | `_process_interceptors()` in `start` |

Container support is built into `Application`. Do **not** add a `deploy_mode` config entry — the pipeline sets it automatically based on `install_manager`.

```python
from jarvis_cd.core.pkg import Application  # or Service / Interceptor / Library
```

## 3. Required Method Overrides

### `_init(self)`
Initialize instance attributes only. `self.config` is not yet populated; default to `None`.

### `_configure_menu(self) -> list[dict]`
Declare configuration parameters. Each entry supports `name`, `msg`, `type` (`str|int|float|bool`), `default`, optional `choices`, `aliases`, `required`.

```python
def _configure_menu(self):
    return [
        {'name': 'nprocs', 'msg': 'Number of MPI processes', 'type': int, 'default': 4},
        {'name': 'ppn',    'msg': 'Processes per node',      'type': int, 'default': 4},
        {'name': 'out',    'msg': 'Output directory',         'type': str, 'default': '/tmp/out'},
    ]
```

If inheriting from `SimplePackage`, do `base = super()._configure_menu(); return base + [...]`.

### `_configure(self, **kwargs)`
**ALL setup work goes here**: env vars, directory creation (local AND remote), template rendering, validation. `self.update_config()` is called for you before `_configure()` runs — do not call it explicitly.

```python
def _configure(self, **kwargs):
    super()._configure(**kwargs)               # Triggers build_phase/build_deploy_phase in container mode
    if self.config.get('deploy_mode') == 'default':
        self.setenv('MY_APP_HOME', self.config['install_path'])
        self.prepend_env('PATH', f"{self.config['install_path']}/bin")
        Mkdir(self.config['out'],
              PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()
```

### `start(self)` — ONLY run programs
No env mutations, no mkdir, no config file writes. Branch on `deploy_mode` for container vs bare metal.

### `stop(self)` / `kill(self)` / `clean(self)` / `status(self) -> str`
Implement as needed. `Application.stop` is usually a no-op; `Service.stop` gracefully ends the daemon; `clean` uses `Rm` on outputs.

### `_get_stat(self, stat_dict)` (optional)
Populate pipeline-test stats. Always prefix keys with `self.pkg_id`:
```python
stat_dict[f'{self.pkg_id}.bandwidth_mb_s'] = 1234.5
stat_dict[f'{self.pkg_id}.runtime'] = self.start_time
```

## 4. Environment — `self.env` vs `self.mod_env`

- `self.env`: shared pipeline env; mutations propagate to later packages. Set via `self.setenv`, `self.prepend_env`.
- `self.mod_env`: deep copy for this package's execution; modified by interceptors at runtime. Pass this to `Exec(..., ExecInfo(env=self.mod_env))`.
- Use `self.track_env({'CUDA_HOME': os.environ.get('CUDA_HOME', '')})` to capture host env values.

Set env in `_configure()` so propagation works. Setting env in `start()` is too late.

## 5. Execution System

```python
from jarvis_cd.shell import Exec, LocalExecInfo, MpiExecInfo, PsshExecInfo, SshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm, Kill, Chmod, Which, GdbServer
```

### The `.run()` Rule — the #1 mistake
`Exec(...)` and all `process` helpers are inert until you call `.run()`. Every single one.
```python
Exec('cmd', LocalExecInfo(env=self.mod_env)).run()   # ✅
Mkdir(path, PsshExecInfo(hostfile=self.hostfile)).run()  # ✅
Rm(path, LocalExecInfo())                                  # ❌ never runs
```

### MPI execution
```python
Exec(cmd, MpiExecInfo(
    nprocs=self.config['nprocs'],
    ppn=self.config['ppn'],
    hostfile=self.hostfile,          # ALWAYS self.hostfile, never self.jarvis.hostfile
    env=self.mod_env,
    cwd=self.config.get('out'),
)).run()
```

`self.jarvis.hostfile` lives in `~/.ppi-jarvis/` which is invisible to containers and remote nodes. `self.hostfile` resolves to a hostfile stored under `self.shared_dir` and is always reachable.

### GdbServer for debugging
Use multi-command form with `disable_preload: True` on the gdbserver entry. When `do_dbg` is false set `nprocs: 0` on gdbserver so MPI skips it.

## 6. Package Directories

| Attribute | Purpose |
|---|---|
| `self.pkg_dir` | Package source dir (read-only). Put templates, static configs, `build.sh`, `Dockerfile.deploy` here. |
| `self.shared_dir` | Pipeline-wide runtime dir; visible from host + every container + every remote node. Put hostfiles, generated configs, cross-package artifacts here. |
| `self.config_dir` | Per-instance runtime dir. Private to one package instance. |
| `self.private_dir` | Per-package scratch dir for files that should be mounted into the container but not shared across packages. |

Use `self.copy_template_file(src, dst, replacements={'VAR': value})` to render `##VAR##` placeholders.

## 7. Container Support (Applications Only)

Container mode is enabled at the pipeline level via `install_manager: container`. To make a package containerizable:

1. Write `build.sh` with the commands that install and compile the software. Use `##VAR##` placeholders.
2. Write `Dockerfile.deploy` template — multi-stage using `##BUILD_IMAGE##` and `##DEPLOY_BASE##`.
3. Override `_build_phase` and `_build_deploy_phase` to return `(content, image_suffix)` tuples (or `None` when not in container mode).
4. Branch `start()` on `self.config.get('deploy_mode') == 'container'`.

### Build flow (what Jarvis does)
1. Start a long-running build container from `container_base` (pipeline-level setting).
2. `docker exec` each package's `build.sh` inside that same container, in pipeline order — later packages see earlier packages' artifacts.
3. `docker commit` → `jarvis-build-{pipeline_name}`.
4. For each non-Library package, build a deploy image from its `Dockerfile.deploy` using `COPY --from=builder`.
5. Merge deploy images into the final `{pipeline_name}` image.
6. Tear down the build container; cleanup intermediates.

### Canonical `build.sh`
```bash
#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git openmpi-bin libopenmpi-dev \
    && rm -rf /var/lib/apt/lists/*

# Compile/install the software into /opt/<name> or /usr/local
```

### Canonical `Dockerfile.deploy`
```dockerfile
FROM ##BUILD_IMAGE## AS builder
FROM ##DEPLOY_BASE##

RUN apt-get update && apt-get install -y --no-install-recommends \
        openmpi-bin libopenmpi3 openssh-server openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/myapp /opt/myapp

# Self-generated SSH keys so all replicas of this image trust each other
RUN mkdir -p /var/run/sshd /root/.ssh && ssh-keygen -A \
    && ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 \
    && cat /root/.ssh/id_ed25519.pub >> /root/.ssh/authorized_keys \
    && chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys \
    && printf "StrictHostKeyChecking no\nUserKnownHostsFile /dev/null\n" >> /root/.ssh/config \
    && chmod 600 /root/.ssh/config

EXPOSE 22
CMD ["/bin/bash"]
```

### Canonical `_build_phase` / `_build_deploy_phase`
```python
def _build_phase(self):
    if self.config.get('deploy_mode') != 'container':
        return None
    content = self._read_build_script('build.sh', {
        'BASE_IMAGE': self.config.get('base_image', 'sci-hpc-base'),
    })
    return content, 'v1'                          # (script, image_suffix)

def _build_deploy_phase(self):
    if self.config.get('deploy_mode') != 'container':
        return None
    content = self._read_dockerfile('Dockerfile.deploy', {
        'BUILD_IMAGE': self.build_image_name(),
        'DEPLOY_BASE': 'ubuntu:24.04',
    })
    return content, 'v1'
```

Helpers:
- `self._read_build_script(path, replacements)` / `self._read_dockerfile(path, replacements)` — both are aliases for `_read_template`; resolve `##VAR##`.
- `self.build_image_name(suffix=None)` / `self.deploy_image_name(suffix=None)` — stable/pipeline-specific names.
- `self._container_engine` — active engine (`docker|podman|apptainer|none`), or `none` when running bare-metal.
- `self.ssh_port` — container SSH port when containerized, else 22.

### Starting inside a container
```python
def start(self):
    if self.config.get('deploy_mode') == 'container':
        Exec(cmd, MpiExecInfo(
            nprocs=self.config['nprocs'],
            ppn=self.config['ppn'],
            hostfile=self.hostfile,
            port=self.ssh_port,
            container=self._container_engine,
            container_image=self.deploy_image_name(),
            shared_dir=self.shared_dir,
            private_dir=self.private_dir,
            env=self.mod_env,
        )).run()
    else:
        Exec(cmd, MpiExecInfo(...)).run()
```

### Wrapper / no-build packages
Return `None` (or `('', suffix)`) from `_build_phase` / `_build_deploy_phase`. Jarvis silently skips them in the build. Useful for `Interceptor` packages whose `.so` comes from a companion runtime package.

### Library packages (hdf5, adios2, compress_libs, …)
Inherit from `Library`. They build their `build.sh` into `/usr/local` (or `/opt/<name>`) so downstream packages get them automatically injected into the build container. Their deploy images are cached but are **not merged** into the final pipeline image.

### Container caching
Set `container_cache: false` on a package in the pipeline YAML to force a rebuild when upstream sources changed but `build.sh` didn't:
```yaml
pkgs:
  - pkg_type: builtin.lammps
    container_cache: false
```

## 8. Interceptor Development

Interceptors modify a target package's environment at runtime (typically via `LD_PRELOAD`). They implement `modify_env()`, not `start()`. Jarvis shares the exact same `mod_env` dict between interceptor and target package, so `self.setenv('LD_PRELOAD', ...)` in the interceptor takes effect for the next package.

```python
class Darshan(Interceptor):
    def _configure_menu(self):
        return [
            {'name': 'library_path', 'msg': 'Interceptor library',
             'type': str, 'default': '/usr/local/lib/libdarshan.so'},
        ]

    def _configure(self, **kwargs):
        super()._configure(**kwargs)
        lib = self.find_library('darshan') or self.config['library_path']
        if not lib:
            raise FileNotFoundError("darshan library not found")
        self.lib = lib

    def modify_env(self):
        current = self.mod_env.get('LD_PRELOAD', '')
        self.setenv('LD_PRELOAD', f"{self.lib}:{current}" if current else self.lib)
```

Pipeline YAML — interceptors are declared at pipeline level and referenced by name:
```yaml
pkgs:
  - pkg_type: builtin.ior
    pkg_name: benchmark
    interceptors: [darshan_interceptor]
interceptors:
  - pkg_type: builtin.darshan
    pkg_name: darshan_interceptor
    library_path: /usr/local/lib/libdarshan.so
```

For an interceptor to work in container mode, the `.so` must be present inside the deploy container — either built in the runtime package's `build.sh` and copied in its `Dockerfile.deploy`, or injected by a companion Library package.

## 9. Spack Support

When `install_manager: spack`, Jarvis:
1. Collects `install:` specs from every package.
2. Runs `spack install` then `spack load` and merges the resulting env.
3. Calls `start()` with `deploy_mode == 'default'` on every package.

No code changes needed — the `install` key is in the common menu. Document the recommended spec in your README.

## 10. Utility Classes

- `SizeType("1G")` / `SizeType("512M")` / `SizeType(bytes_int)` — bidirectional size parsing and formatting. Supports arithmetic, comparisons, `.bytes`, `.kilobytes`, `.megabytes`, `.gigabytes`, `.terabytes`, and convenience functions `size_to_bytes`, `human_readable_size`.
- `self.log(message, color=Color.GREEN)` — colored, prefixed console output.
- `self.sleep(seconds)` — logged delay; falls back to `self.config['sleep']`.
- `self.copy_template_file(src, dst, replacements)` — render `##VAR##` templates.
- `self.find_library('name')` — search `LD_LIBRARY_PATH` and standard paths for a shared library.

## 11. Best Practices — Hard Rules

1. **Every `Exec(...)` / `Mkdir(...)` / `Rm(...)` / `Kill(...)` / `Which(...)` must end in `.run()`.** Missing `.run()` means the command never executes — the #1 bug.
2. **`_configure` does setup, `start` does execution.** Environment variables, directory creation, config file generation, and validation belong in `_configure` — not in `start`. `start` may be called multiple times after `stop`.
3. **Always use `self.hostfile` in Exec calls**, never `self.jarvis.hostfile`. The former is in `shared_dir` and visible to containers and remote nodes.
4. **Use Jarvis execution classes**, not `subprocess.run` or `os.system`. The framework handles env propagation, remote execution, MPI wrapping, and container wrapping.
5. **Do not add `deploy_mode` to `_configure_menu`.** The pipeline sets it automatically from `install_manager`.
6. **Set env in `_configure`, not `start`.** Only `_configure` env mutations propagate to later packages.
7. **Branch on `deploy_mode == 'container'`** for any container-aware code. `deploy_mode == 'default'` means bare-metal / Spack.
8. **Class name must match directory name in PascalCase.** Loader will hard-fail otherwise.
9. **Library packages install to `/usr/local`** (or `/opt/<name>`) so downstream packages pick them up via build-container injection.
10. **Container caches aggressively.** Use `container_cache: false` to force a rebuild when source repos changed.

## 12. Starting a New Package — Quick Recipe

```python
"""
<one-liner>: what this package runs and why.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo, LocalExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class MyPackage(Application):
    """<class docstring>"""

    def _init(self):
        pass

    def _configure_menu(self):
        return [
            {'name': 'nprocs', 'msg': 'Number of MPI processes', 'type': int, 'default': 4},
            {'name': 'ppn',    'msg': 'Processes per node',      'type': int, 'default': 4},
            {'name': 'out',    'msg': 'Output directory',         'type': str, 'default': '/tmp/my_out'},
            {'name': 'base_image', 'msg': 'Base build image',     'type': str, 'default': 'sci-hpc-base'},
        ]

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        return self._read_build_script('build.sh', {
            'BASE_IMAGE': self.config.get('base_image', 'sci-hpc-base'),
        }), 'v1'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        return self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        }), 'v1'

    def _configure(self, **kwargs):
        super()._configure(**kwargs)
        if self.config.get('deploy_mode') == 'default':
            Mkdir(self.config['out'],
                  PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

    def start(self):
        cmd = 'my_binary --out ' + self.config['out']
        if self.config.get('deploy_mode') == 'container':
            Exec(cmd, MpiExecInfo(
                nprocs=self.config['nprocs'], ppn=self.config['ppn'],
                hostfile=self.hostfile, port=self.ssh_port,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir, private_dir=self.private_dir,
                env=self.mod_env)).run()
        else:
            Exec(cmd, MpiExecInfo(
                nprocs=self.config['nprocs'], ppn=self.config['ppn'],
                hostfile=self.hostfile, env=self.mod_env,
                cwd=self.config['out'])).run()

    def stop(self):
        pass

    def clean(self):
        Rm(self.config['out'] + '*',
           PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()
```

Then put `build.sh` and `Dockerfile.deploy` alongside `pkg.py`, and you have a fully dual-mode (bare-metal + container) package.

---

## When Reviewing a Package

Checklist to run through:
- [ ] Class name matches directory in PascalCase
- [ ] Inherits from correct base class
- [ ] `_configure_menu` returns a list of dicts, no `deploy_mode` entry
- [ ] `_configure` calls `super()._configure(**kwargs)` before its own work
- [ ] All directory creation, env mutations, and validation live in `_configure`, not `start`
- [ ] `start` only runs programs; branches on `deploy_mode` if container-aware
- [ ] Every `Exec/Mkdir/Rm/Kill/Which` call ends in `.run()`
- [ ] MPI calls use `self.hostfile`, not `self.jarvis.hostfile`
- [ ] Container code: `_build_phase` / `_build_deploy_phase` return `None` outside container mode
- [ ] `build.sh` is idempotent, runs as root inside the build container, and uses `##VAR##` placeholders
- [ ] `Dockerfile.deploy` uses `##BUILD_IMAGE##` and `##DEPLOY_BASE##` placeholders and generates SSH keys
- [ ] `clean` removes only this package's output; uses `PsshExecInfo(hostfile=self.hostfile)` for multi-node cleanup
- [ ] No `subprocess.run` / `os.system` / `mkdir` calls outside the Jarvis API
- [ ] Imports follow: `from jarvis_cd.core.pkg import …` and `from jarvis_cd.shell import …`

## Reference Packages (in `builtin/builtin/`)

Study these when picking a pattern:
- `ior/` — canonical MPI benchmark with build.sh + Dockerfile.deploy
- `lammps/` — MPI sim with GPU/CPU variant suffixes
- `ai_training/` — multi-stage CUDA container with training script bundled in
- `wrf_container/`, `xcompact3d/`, `openfoam/`, `gray_scott_paraview/` — scientific simulations
- `hdf5/`, `adios2/` — `Library` packages
- `darshan/`, `example_interceptor/` — interceptors via `modify_env`

When in doubt, mimic the closest existing package rather than inventing new patterns.
