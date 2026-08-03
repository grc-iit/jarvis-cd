# ADIOS2 PDF Calc

This Jarvis package runs the PDF Calc application, which analyzes Gray-Scott simulation output and computes the probability distribution function (PDF) for each 2D slice of the U and V variables.

## Description

PDF Calc reads ADIOS2 data produced by the Gray-Scott simulation and computes statistical distributions. It's designed to run as a consumer in a producer-consumer workflow with Gray-Scott.

## Prerequisites

- Gray-Scott simulation output (e.g., `gs-output.bp`)
- pdf_calc binary built and available in PATH

## Configuration Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `nprocs` | int | 2 | Number of MPI processes |
| `ppn` | int | 16 | Processes per node |
| `executable` | str | `pdf_calc` | Installed executable resolved through `PATH` |
| `input_bundle` | str | empty | Optional digest-verified source bundle |
| `input_file` | str | *Required* | Input file from Gray-Scott simulation |
| `output_file` | str | *Required* | Output file for PDF analysis results |
| `nbins` | int | 1000 | Number of bins for PDF calculation |
| `output_inputdata` | str | NO | Write original variables (YES/NO) |

## Usage

### Digest-verified source build

When `pdf_calc` is not installed, `input_bundle` may point to a regular tar
archive using `jarvis.package-input-bundle.v1`. Its manifest must declare
exactly one `build_spec` and one `adios2_configuration`. JARVIS verifies the
complete archive before submission, then builds the `pdf_calc` target in an
execution-owned workspace using CMake, MPI compilers, and the active ADIOS2
development environment.

```bash
jarvis ppl append adios2_pdf_calc \
  input_bundle=/staged/gray-scott-source.tar \
  input_file=/shared/gray-scott/feed-low.bp \
  output_file=feed-low-pdf.bp \
  nbins=128 nprocs=1 ppn=1
```

Relative output paths are scoped to the PDF Calc package shared directory.
Absolute input paths allow explicit consumption of a prior pipeline product.
There is no implicit developer checkout, and a nonzero `pdf_calc` exit fails
the package lifecycle.

### Standalone Usage

```bash
# Create and configure the package
jarvis ppl create pdf-workflow
jarvis ppl append adios2_pdf_calc \
  --input_file=/path/to/gs-output.bp \
  --output_file=/path/to/pdf-output.bp \
  --nbins=1000 \
  --nprocs=2

# Run the analysis
jarvis ppl start
```

### In a Complete Workflow

```yaml
name: gray-scott-pdf-workflow
hostfile: null
interceptors: []

environment:
  PATH: /workspace/external/iowarp-gray-scott/build/bin:${PATH}

pkgs:
  # Producer: Gray-Scott Simulation
  - pkg_type: builtin.adios2_gray_scott
    L: 64
    steps: 100
    plotgap: 5
    nprocs: 2
    engine: bp5
    out_file: /workspace/external/iowarp-gray-scott/gs-output.bp
    db_path: /workspace/external/iowarp-gray-scott/metadata.db
    full_run: false

  # Consumer: PDF Analysis
  - pkg_type: builtin.adios2_pdf_calc
    input_file: /workspace/external/iowarp-gray-scott/gs-output.bp
    output_file: /workspace/external/iowarp-gray-scott/pdf-output.bp
    nbins: 100
    nprocs: 2
```

Then run:
```bash
jarvis ppl run yaml my-workflow.yaml
```

## Command Line Reference

The underlying command executed by this package is:
```bash
mpirun -n <nprocs> pdf_calc <input_file> <output_file> <nbins> [output_inputdata]
```

## Notes

- The `input_file` and `output_file` parameters are required
- Ensure the pdf_calc binary is in your PATH before running
- The package runs as part of a pipeline and can be combined with gray-scott
- Output files are cleaned up by the `clean` method

## Example

```bash
# Run Gray-Scott first
jarvis ppl load yaml gray-scott.yaml
jarvis ppl start

# Then run PDF Calc
jarvis ppl load yaml pdf-calc.yaml
jarvis ppl start

# Check results
ls -lh pdf-output.bp/
```
