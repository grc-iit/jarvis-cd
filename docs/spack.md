# Spack site preparation (`jarvis spack`)

The `spack` menu prepares the JARVIS Spack (`JARVIS_SPACK_ROOT`) for a site,
so that [find-first installs](pipeline_tests.md#find-first-spack-installs)
reuse what the site already has instead of rebuilding it. It replaces the
site preparation that callers used to do themselves (clio-kit's
`spack_prepare_site`).

| Command | Purpose |
|---------|---------|
| `jarvis spack prepare [options] [+json]` | Chain the JARVIS Spack to a site Spack and write its site configuration |

## `jarvis spack prepare`

```bash
export JARVIS_SPACK_ROOT=$HOME/jarvis-cd/spack
jarvis spack prepare --site_spack /opt/spack +json
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--root` | `$JARVIS_SPACK_ROOT` | The JARVIS Spack to prepare. One of the two is required |
| `--site_spack` | none | A site Spack checkout to chain as an upstream. It must have `bin/spack` and must not be the root itself |
| `--spack_version` | the site Spack's release | The Spack release for the root. Required when there is no site Spack |
| `--install_tree` | asked from the site Spack (`spack.store.STORE.root`) | The site's install tree. It must contain `.spack-db/index.json` |
| `--module_paths` | `$MODULEPATH` from a login shell | Colon-separated module directories to search for MPI modules |
| `--build_tools` | autoconf, automake, bison, cmake, diffutils, findutils, flex, gawk, git, gmake, libtool, m4, ninja, perl, pkgconf, tar | Comma-separated tools to register as non-buildable externals, or `none`. An MPI is refused here |
| `--compiler` | chosen by policy | The preferred compiler (`gcc@11.4.0`), or `auto` to forget an earlier choice. It answers `ambiguous_compiler` |
| `--build_jobs` | 1024 (every CPU the build job gets) | `config:build_jobs` |
| `--answers` | none | An `answers.json` keyed by `issue_id`, as for `ppl install` |
| `+no_clone` | off | Refuse instead of cloning a missing root |
| `+json` | off | Print the `jarvis.spack-site-prep.v1` record as one JSON line |

`spack prepare` does not need an initialized `JARVIS_ROOT`: it works only on
the Spack root, so a relay can run it before `jarvis init`.

### What it does

1. **The checkout.** When the root has no `bin/spack`, it makes a shallow
   `git clone --branch v<version>` of github.com/spack/spack. The version is
   the site Spack's release (read from its source tree), or
   `--spack_version`. Known releases are checked against a pinned commit. A
   root that already holds a different release, or a non-empty directory
   that is not a Spack, is refused and left alone.
2. **The site scope.** Everything is written to `<root>/etc/spack`, the scope
   that survives `SPACK_DISABLE_LOCAL_CONFIG=1` (see
   [Spack example](pipeline_tests.md#spack-example)).
   Each file is rewritten only when its content changes:
   - `upstreams.yaml`: the site Spack's install tree, plus its `lmod` and
     `tcl` module trees when they exist;
   - `config.yaml`: `misc_cache`, `test_stage` and the bootstrap root under
     `<root>/var/jarvis`, `build_stage` under `$tempdir/$user`, and
     `build_jobs`;
   - `concretizer.yaml`: `reuse: true`;
   - `bootstrap.yaml`: a private bootstrap root.
3. **Compilers and build tools.** `spack compiler find --scope site`, then
   `spack external find --not-buildable --scope site <build tools>`.
4. **MPI.** `packages:mpi:buildable:false`, so an MPI is never built. Every
   site MPI module (`<modulepath>/<openmpi|mpich|…>/<version>[.lua]`, outside
   Spack's own module trees) is loaded in a throwaway login shell. The prefix
   of the `mpiexec` it puts first on `PATH` is recorded as an external
   `{spec: <name>@<version>, prefix, modules: [<module>]}`. Existing
   externals are kept, and a prefix that differs from the module's is
   reported as a warning. The install's MPI choice then offers these modules
   next to the upstream installs (see `ambiguous_provider` in
   [pipeline_tests.md](pipeline_tests.md#find-first-spack-installs)).
5. **Compiler preference.** `packages:all:compiler` lists the compilers in
   order. Full toolchains (cc, cxx and fc) come first, and among them the one
   the upstream MPI was built with. The first one is the preferred compiler,
   picked in this order:
   1. `--compiler`, or an answer to `ambiguous_compiler`. It is saved in
      `<root>/var/jarvis/compiler-choice` (`source: person` or `answer`).
   2. An earlier saved choice (`source: person_earlier`). A saved compiler
      that is gone is forgotten, with a warning.
   3. The single full toolchain that built an upstream MPI
      (`upstream_mpi`), or the only full toolchain (`only_full_toolchain`).
   4. With no full toolchain at all, the first compiler, with a warning
      that Fortran packages cannot build (`no_full_toolchain`).

   When several full toolchains qualify and nothing above picks one, JARVIS
   asks instead of guessing: the command exits `3` with an
   `ambiguous_compiler` issue (`jarvis.install-issue.v1`, `subject:
   compiler`, `scope: site`). Its `candidates[]` are `{id, constraint, cc,
   fc, built_upstream_mpi}`. Answer with `--compiler <constraint>` or with
   `--answers answers.json`.

   A person's choice (`source` `person`, `answer` or `person_earlier`) is
   binding: besides the order, prepare writes `packages:all:require`
   (`%<compiler>`, or `%[when=%c] c=<compiler>` on Spack 1.x), so the
   concretizer can't move a build to another compiler, even to reuse an
   upstream spec. A compiler JARVIS picked by policy only sets the order. A
   `require` that prepare wrote earlier is removed when the choice is no
   longer a person's. The record's `compiler.binding_require` holds it.

Prepare runs on the login node, so the compilers it registers are the
login node's. A compiler can still be missing where builds run (for example
`/usr/bin/g++-12` on the login node only). Each install checks this again on
the build host; see [Compilers on the build host](#compilers-on-the-build-host).

It never installs or builds. Its Spack calls are configuration writes plus
`compiler find`, `external find`, `config get` and `find`, each with a
timeout. If a site allows nothing but `spack find` and `spack spec` on its
login nodes, run it inside a JARVIS-submitted job.

### The record

The result is written to `<root>/var/jarvis/site-prep.json`
(`jarvis.spack-site-prep.v1`) and printed with `+json`:

| Field | Meaning |
|-------|---------|
| `status`, `exit_code` | `prepared` (0) or `needs_decision` (3) |
| `root`, `version`, `commit`, `pinned`, `checkout` | the JARVIS Spack, its release and commit, the expected pin, and `present` or `cloned` |
| `config_scope` | `<root>/etc/spack` |
| `site_spack`, `site_version`, `upstream_install_tree` | the chained site Spack |
| `build_jobs`, `build_tools` | as configured |
| `changes[]` | each file written or unchanged, and each policy change |
| `module_mpis[]` | `{name, version, module, prefix, modulepath}` found |
| `mpi_externals_added[]` | the specs added to `packages.yaml` |
| `installed_mpis[]` | `{name, version, hash, compiler, prefix}` from `spack find` |
| `compiler` | `chosen`, `source`, `full_toolchain`, `order`, `toolchains[]`, `choice_needed`, `candidates` |
| `issues[]`, `open_issues[]` | the open `ambiguous_compiler` question, if any |
| `compiler.binding_require` | the `packages:all:require` written for a person's choice, or `null` |
| `environment` | `{"JARVIS_SPACK_ROOT": <root>}`, to export for later runs |
| `commands[]` | every command it ran, with its exit code |
| `warnings[]`, `prepared_at`, `record_path` | |

Every later `install-record.json` written with that root carries a
`spack_site_prep` summary (path, time, version, commit, site Spack, upstream
tree, chosen compiler and status), and the run's
[provenance record](executions.md#provenance-record) repeats it as
`spack.site_prep`.

### Refusals

A refusal exits `5` with a `jarvis.issue.v1` (`subject: spack_site_prep`)
and changes nothing that was valid:

| `kind` | When |
|--------|------|
| `spack_root_unset` | no `--root` and no `JARVIS_SPACK_ROOT` |
| `site_spack_invalid` | `--site_spack` has no `bin/spack`, or is the root itself |
| `site_install_tree_invalid` | the install tree has no `.spack-db/index.json` |
| `spack_version_unknown`, `spack_version_invalid` | no release is known, or it is not `X.Y[.Z]` |
| `spack_version_mismatch` | the root already holds another release |
| `spack_root_not_spack`, `spack_root_missing` | the root is not a Spack, or is missing with `+no_clone` |
| `spack_clone_failed`, `spack_pin_mismatch` | the clone failed, or its commit is not the release's pin |
| `build_tool_invalid` | a build tool is not a package name, or is an MPI |
| `compiler_invalid`, `compiler_not_found` | `--compiler` is malformed, or matches no compiler (or more than one) |
| `build_jobs_invalid` | `--build_jobs` outside 1..1024 |
| `answer_invalid` | an answer is not one of the candidates |
| `spack_compiler_find_failed`, `spack_external_find_failed` | Spack itself failed |

### Compilers on the build host

`spack prepare` registers the compilers it finds on the login node, but
builds run on compute nodes. An install checks every compiler where it
builds, before any `spack install`:

1. **Verify.** In install mode (normally the `ppl install +submit` job on a
   compute node), JARVIS reads `spack config get compilers` (or the compiler
   externals in `packages:` on Spack 1.x). It checks that each configured
   `cc`, `cxx`, `f77` and `fc` is an executable file on that host, which is
   the same test behind Spack's `CompilerAccessError`. Then it **probes**
   each compiler that passed: in a temp dir it compiles, links and runs a
   trivial program per language (C with `cc`, C++ with `cxx`, Fortran with
   `fc`), with the compiler entry's `environment` applied. This is the
   check a configure or CMake step makes. Each language's result is in
   `probe_results`: `ok`, the `step` that ran last (`setup`,
   `write_source`, `compile` or `run`), its `command`, `cwd`,
   `exit_code`, `output` and `error`. Languages that failed are listed in
   `probe_failed`. The probe is evidence, not a verdict: a test program can
   fail for many reasons (the toolchain, the node's disk, its environment),
   so it changes neither usability nor `languages`. Only a missing
   executable makes a compiler unusable. A compiler's `languages` are those
   with a configured path. A compiler loaded through `modules` is not
   checked. Set
   `JARVIS_COMPILER_PROBE=off` to keep only the existence check. The result
   (`jarvis.build-compilers.v1`, with `probed`, `probe_failed`, and
   `missing`/`probe_results`/`languages` per compiler) goes to
   `<install>/build-compilers.json` and to
   `<JARVIS_SPACK_ROOT>/var/jarvis/build-compilers/<host>.json`, one file
   per node. A `+check` on the login node uses the newest node's
   missing-executable facts
   (`source: recorded`) rather than   (`source: recorded`) rather than the login node's compilers. For a
   scheduled pipeline it runs no Spack at all and reports the compilers
   from the last full check's `install-check.json` (see
   [scheduler.md → Light checks](scheduler.md#light-checks-on-the-submitting-host)). With no
   result yet, it trusts the configured compilers. Compiler usability is
   **per node**: compute nodes of one cluster can differ (one has `g++-12`,
   the next only the `gcc-12` C driver). So a check's result is not a
   promise for the node an install job lands on. Every install job
   re-checks its own node, and `build_compilers.per_node` (also printed by
   `+check` and returned in its `+json` installer result) says which host
   and time the shown result comes from.
2. **Exclude.** Every Spack call in the install gets a `spack -C` scope
   (`<install>/spack-compiler-scope`), and the pipeline environment merges
   the same sections:
   - `compilers:` lists only the usable compilers.
   - `concretizer:reuse:` excludes specs built with an unusable compiler:
     `from: [{type: local, exclude: ["%gcc@12.3.0"]}, {type: buildcache,
     exclude: […]}, {type: external}]`. This needs Spack 0.22 or later.
   - `packages:all:require` binds the person's compiler, when there is one.
   This matters because the concretizer prefers reuse over the compiler
   order: one reused upstream spec built with gcc 12 (`gcc-runtime`,
   `glew`, `osmesa`) pulls the rest of the DAG onto `%gcc@12.3.0`. Excluding
   that reuse means those upstream specs are rebuilt with a usable compiler.
   They show up in the plan's `missing` nodes, and the record lists the
   exclusions in `reuse_excluded`.
3. **Languages.** A compiler is usable for a build only if it has every
   language the build needs. After the first plan, the install asks Spack
   which language virtuals (`depends_on("c")`, `"cxx"`, `"fortran"`) each
   node to build declares. It runs one `spack python` script
   (`<install>/spack-languages.py`), which evaluates each dependency's
   `when` against the node's concretized spec. The build needs the union:
   for example xcompact3d needs Fortran, and ParaView needs C++. A usable
   compiler without one of those languages is left out of this build's
   `compilers:` with the reason `no <lang> compiler on <host>`. Specs
   already built with it stay reusable, because a language gap doesn't
   break reuse. When a node's languages are unknown (a Spack without
   language virtuals and a package that declares none, a package Spack
   can't load, or the script failed), JARVIS prefers compilers that have
   `cc`, `cxx` and `fc` if there is one. If the first plan had put a node
   to build on a compiler that was left out, the packages are planned
   again with the narrowed scope, and the first pass's questions are
   withdrawn (`replanned: true`). If no usable compiler has every needed
   language (or none has them all at once), the install stops before any
   `spack install` with `compiler_unavailable`. That issue carries
   `missing_languages`, `needed_languages` (language to nodes) and
   `usable_compilers` with their `languages`. It is not answerable (no
   candidate exists): register a compiler with that language on the build
   nodes, or pin the install job to nodes that have one
   ([`scheduler.install`](scheduler.md) `nodelist` or `constraint`). If
   the binding compiler (step 5) lacks a needed language, the install
   asks `compiler_unavailable` over the compilers that have them all.
4. **Guard.** After `spack spec`, and again after the environment's
   `concretize`, any node still to build with a compiler whose executables
   are missing on the build host stops the install with a
   `compiler_unavailable` issue (exit `3`). No `spack install` runs. The
   issue names the host, the compiler, its `missing_executables`, the
   nodes `to_build`, and the `reused_specs` built with that compiler that
   pulled the DAG there. This happens when the Spack predates reuse
   filters, or when a requirement forces that compiler. A node to build
   whose compiler failed the C test program on this host (in the install
   job, `source: verified`) stops the install with a `failure` record
   instead (exit `6`, `stage: compiler_probe`): the `compiler`, `language`,
   `probe_step`, the `node`, the probe's `command`, `exit_code` and
   `error`, and its full output in a file (see
   [Install failures](pipeline_tests.md#install-failures)). JARVIS asks no
   compiler question for it. A C++ or Fortran failure, or one on a
   compiler nothing to build uses, stays evidence in `build_compilers`. A
   check reads probe results as evidence only, so a retry whose install
   job lands on another node probes that node and goes ahead when it
   passes.
5. **Bind.** A person's compiler (site prep `--compiler`, an
   `ambiguous_compiler` answer, or an answer to `compiler_unavailable`) is a
   `require`, not a preference. If that compiler is itself unusable on the
   build host, the install asks again with `compiler_unavailable`
   (`origin: build host check`), with the usable compilers as candidates.
   An answer to `compiler_unavailable` is recorded in `decisions.yaml` and
   becomes the binding `require`.
6. **A failed build.** If a build fails anyway, the failure is the
   `failure` record of that `spack install`, with its output and the
   package's build log (see
   [Install failures](pipeline_tests.md#install-failures)). JARVIS does not
   read a compiler into it or take a compiler out because of it.

`install-record.json` carries the outcome as `build_compilers`:

| Field | Meaning |
|-------|---------|
| `source` | `verified` (checked on this host) or `recorded` (the last build-host check) |
| `host`, `checked_at`, `allocation` | where and when the compilers were checked |
| `probed` | whether the build-test probe ran (`false` with `JARVIS_COMPILER_PROBE=off`, or for a recorded check from before the probe) |
| `usable`, `unusable[]` | specs; `unusable[]` is `{spec, missing}` (its missing executables) |
| `probe_failed[]` | `{spec, languages}` for each usable compiler whose test program failed on that host; the per-language `probe_results` are in `path` |
| `excluded`, `reuse_excluded` | compilers left out of the scope (those with missing executables, and those missing a language this build needs), and the `concretizer:reuse` excludes |
| `languages` | each compiler's languages (`c`, `cxx`, `fortran`) on the checked host |
| `build_languages` | `{method, error, needed: {lang: [nodes]}, unknown: [nodes], prefer_full, allowed, excluded}` from the language step (install mode, when something is missing) |
| `language_excluded[]` | `{spec, missing_languages, reason, needed_by, note?}`; `reason` is `<spec>: no <lang> compiler on <host>` |
| `replanned` | `true` when the language step re-planned the packages |
| `per_node` | why the shown usability holds for one node (`host`, `checked_at`), not as a promise for others |
| `required` | `{spec, source, require}` for the binding compiler, or `null` |
| `blocked_by` | the `compiler_unavailable` `issue_id` that stopped the install, or `null` |
| `scope`, `path` | the `-C` scope dir and the build-compilers file used |

A build that fails anyway is reported as a `failure` record (exit `6`); see
[Install failures](pipeline_tests.md#install-failures).

### Finding a site Spack

When JARVIS has no Spack at all, `ppl install [+check]` and
[`jarvis sched info`](sched.md) report a `spack_missing` issue. Its
`site_spacks[]` candidates each carry a `prepare` command
(`jarvis spack prepare --site_spack <root>`). See
[Missing Spack](pipeline_tests.md#missing-spack-spack_missing).
