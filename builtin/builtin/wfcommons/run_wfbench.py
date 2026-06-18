#!/usr/bin/env python3
"""
Driver: generate a wfcommons recipe-based benchmark workflow, translate
it into a self-contained bash workflow, and execute it.

Uses the real wfcommons 1.x API:

    WorkflowBenchmark(recipe=<RecipeCls>, num_tasks=N)
        .create_benchmark(save_dir, cpu_work=..., data=...)   -> path to JSON
    BashTranslator(workflow=<json_path>).translate(output_folder=...)
        -> writes run_workflow.sh + bin/{wfbench,cpu-benchmark} + data/

The translator's `bin/wfbench` script picks up its shebang from the
`wfbench` interpreter on PATH at translation time. Run this driver
under the same venv that has wfcommons installed so that shebang
resolves to a python that exists inside this environment.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Block-buffered stdout hides failures: when the driver fails mid-run,
# the tail of stderr (Traceback) lands in the log immediately while the
# trailing stdout prints arrive only when the interpreter exits, which
# scrambles the apparent ordering. Force line-buffering up front.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


RECIPE_IMPORTS = {
    "montage": "MontageRecipe",
    "genome": "GenomeRecipe",
    "cycles": "CyclesRecipe",
    "blast": "BlastRecipe",
    "bwa": "BwaRecipe",
    "srasearch": "SrasearchRecipe",
    "epigenomics": "EpigenomicsRecipe",
    "seismology": "SeismologyRecipe",
    "soykb": "SoykbRecipe",
    "rnaseq": "RnaseqRecipe",
}


def load_recipe(name: str):
    import wfcommons
    attr = RECIPE_IMPORTS.get(name.lower())
    if not attr or not hasattr(wfcommons, attr):
        raise RuntimeError(
            f"unknown recipe '{name}'. choices: {sorted(RECIPE_IMPORTS)}"
        )
    return getattr(wfcommons, attr)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--recipe", required=True)
    p.add_argument("--num-tasks", type=int, required=True)
    p.add_argument("--cpu-work", type=int, default=100,
                   help="cpu_work units passed to wfbench tasks")
    p.add_argument("--data", type=int, default=0,
                   help="total workflow data footprint in bytes "
                        "(0 = recipe defaults). Converted to MB internally "
                        "before being passed to WorkflowBenchmark.create_benchmark, "
                        "whose `data` arg is documented as 'Total workflow data "
                        "footprint (in MB)'.")
    p.add_argument("--percent-cpu", type=float, default=0.6)
    p.add_argument("--out", type=str, required=True,
                   help="output dir; receives bench/ and bench/bash/")
    p.add_argument("--clio-prefix", action="store_true",
                   help="rewrite every input/output path the translated "
                        "run_workflow.sh hands to wfbench so it begins "
                        "with 'clio::'. The WRP CTE POSIX adapter, when "
                        "LD_PRELOAD'd, only intercepts paths with that "
                        "prefix; this flag is the opt-in for actually "
                        "routing wfbench's data I/O through CTE.")
    args = p.parse_args()

    from wfcommons.wfbench import WorkflowBenchmark, BashTranslator

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    bench_dir = out_dir / "bench"
    bench_dir.mkdir(parents=True, exist_ok=True)

    recipe_cls = load_recipe(args.recipe)
    # wfcommons.WorkflowBenchmark.create_benchmark expects `data` in MB
    # (its docstring: "Total workflow data footprint (in MB)"). The
    # driver accepts bytes for consistency with the pkg.py SizeType
    # parsing of values like "20G"; convert here.
    data_mb = (args.data + (1024 * 1024) - 1) // (1024 * 1024) if args.data else 0
    print(f"[wfbench] recipe={args.recipe} num_tasks={args.num_tasks} "
          f"cpu_work={args.cpu_work} data={args.data}B (={data_mb} MB)",
          flush=True)

    bm = WorkflowBenchmark(recipe=recipe_cls, num_tasks=args.num_tasks)
    create_kwargs = dict(
        save_dir=bench_dir,
        cpu_work=args.cpu_work,
        percent_cpu=args.percent_cpu,
    )
    if data_mb > 0:
        create_kwargs["data"] = data_mb
    json_path = bm.create_benchmark(**create_kwargs)
    print(f"[wfbench] generated benchmark JSON: {json_path}", flush=True)

    bash_dir = bench_dir / "bash"
    # BashTranslator.translate calls Path.mkdir(parents=True) without
    # exist_ok, so a leftover bash/ from a prior failed run will fail
    # the whole pipeline. Clean it up before translating.
    #
    # ignore_errors: on NFS, files still held open by a prior wfbench
    # worker become .nfs* "silly-rename" placeholders that can't be
    # unlinked until the holder closes them. shutil.rmtree raises on
    # those, but the directory will be empty enough for the translator
    # once normal entries are gone — fall back to renaming any
    # leftover dir out of the way.
    if bash_dir.exists():
        print(f"[wfbench] removing stale {bash_dir}", flush=True)
        shutil.rmtree(bash_dir, ignore_errors=True)
        if bash_dir.exists():
            stash = bash_dir.with_name(
                bash_dir.name + ".stale." + str(int(time.time())))
            print(f"[wfbench] couldn't fully remove (NFS .nfs* locks?); "
                  f"renaming to {stash}", flush=True)
            bash_dir.rename(stash)

    print(f"[wfbench] translating to bash workflow under {bash_dir}", flush=True)
    BashTranslator(workflow=json_path).translate(output_folder=bash_dir)

    runner = bash_dir / "run_workflow.sh"
    if not runner.exists():
        raise RuntimeError(f"translator did not produce {runner}")

    if args.clio_prefix:
        # Discover the set of paths the workflow actually uses (so we
        # don't hardcode 'data/'): collect every path the WfFormat JSON
        # mentions under task `files`, then rewrite each occurrence in
        # run_workflow.sh's JSON-encoded --input-files / --output-files
        # args. Sort by length descending so a path that's a prefix of
        # another doesn't shadow it (e.g. "data/foo" before "data/foo_2").
        with json_path.open() as fh:
            workflow_doc = json.load(fh)
        tasks = (workflow_doc.get("workflow", {}).get("specification", {})
                              .get("tasks") or
                 workflow_doc.get("workflow", {}).get("tasks") or
                 workflow_doc.get("tasks") or [])
        paths = set()
        for t in tasks:
            for f in t.get("inputFiles", []) or t.get("input_files", []) or []:
                if isinstance(f, dict) and "name" in f:
                    paths.add(f["name"])
                elif isinstance(f, str):
                    paths.add(f)
            for f in t.get("outputFiles", []) or t.get("output_files", []) or []:
                if isinstance(f, dict) and "name" in f:
                    paths.add(f["name"])
                elif isinstance(f, str):
                    paths.add(f)
        # BashTranslator stages all files under bash/data/; the strings
        # baked into run_workflow.sh are JSON-encoded with that prefix.
        candidates = sorted(
            {f"data/{p}" for p in paths} | paths,
            key=len, reverse=True,
        )
        with runner.open() as fh:
            text = fh.read()
        rewritten = 0
        for raw in candidates:
            # Paths appear inside escaped JSON: '...\"data/foo\"...'.
            # Match the literal escaped-quote + path + escaped-quote and
            # prepend 'clio::' once.
            pattern = re.compile(r'(\\")' + re.escape(raw) + r'(\\")')
            text, n = pattern.subn(r'\1clio::' + raw + r'\2', text)
            rewritten += n
        with runner.open("w") as fh:
            fh.write(text)
        print(f"[wfbench] clio-prefix: rewrote {rewritten} path "
              f"occurrences in {runner}", flush=True)

    # The translator's bin/wfbench is copied from the python interpreter
    # that ran the translator. When we run inside an apptainer SIF, its
    # absolute shebang naturally points at /opt/wfcommons-env/bin/python3,
    # which exists. Sanity check.
    wfbench_bin = bash_dir / "bin" / "wfbench"
    if wfbench_bin.exists():
        with wfbench_bin.open() as fh:
            shebang = fh.readline().rstrip()
        print(f"[wfbench] bin/wfbench shebang: {shebang}", flush=True)

    print(f"[wfbench] executing {runner}", flush=True)
    rc = subprocess.run(
        ["bash", "run_workflow.sh"],
        cwd=str(bash_dir),
    ).returncode
    if rc != 0:
        print(f"[wfbench] run_workflow.sh exited with rc={rc}", file=sys.stderr)
        sys.exit(rc)
    print(f"[wfbench] OK", flush=True)


if __name__ == "__main__":
    main()
