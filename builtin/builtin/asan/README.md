Hermes is a multi-tiered I/O buffering platform. This is module encompasses the
interceptors (MPI-IO, STDIO, POSIX, and VFD) provided in Hermes.

# Installation

```bash
spack install hermes@master
```

```bash
jarvis pipeline create hermes
jarvis pipeline append hermes --sleep=5
jarvis pipeline append hermes_api +posix -mpich
jarvis pipeline run
```