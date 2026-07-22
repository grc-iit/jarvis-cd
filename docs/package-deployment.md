# Package deployment and readiness contracts

JARVIS packages can publish a versioned, machine-readable deployment contract
through `Pkg.describe_deployment()`. Generic clients can use this document to
select a batch or service workflow, resolve missing software, validate
conditional configuration, and choose the correct readiness observation. They
do not need application-specific instructions in their system prompt.

Packages that have not adopted this interface return `None`. Callers must not
infer deployment behavior from a package class name, source directory, README,
or configuration parameter names.

## Version 1 document

The current schema is `jarvis.package-deployment.v1`:

```json
{
  "schema_version": "jarvis.package-deployment.v1",
  "package": "example.simulator",
  "execution_profiles": [
    {
      "name": "generated_workload",
      "execution_kind": "batch",
      "when": [
        {"parameter": "script", "operator": "is_empty"}
      ],
      "runtime_requirements": ["simulator"],
      "readiness": {
        "mechanism": "process_exit",
        "condition": "successful_exit"
      }
    }
  ],
  "runtime_requirements": [
    {
      "id": "simulator",
      "description": "Runtime able to execute the simulation",
      "required_capabilities": ["parallel_execution"],
      "available_capabilities": [],
      "status": {
        "state": "unavailable",
        "usable": false,
        "reason_code": "software_not_found"
      },
      "provider_resolutions": [
        {
          "provider": "spack",
          "query": {"kind": "spec", "value": "simulator"}
        }
      ]
    }
  ],
  "configuration_rules": [
    {
      "when": [
        {"parameter": "script", "operator": "is_empty"}
      ],
      "requires": [
        {"parameter": "steps", "operator": "greater_than", "value": 0}
      ],
      "description": "The generated workload requires a positive duration."
    }
  ]
}
```

`status` is a snapshot of the package's bounded readiness probe in the effective
JARVIS execution environment. `usable` is `true`, `false`, or `null` when the
package cannot determine current usability. A provider resolution is an
optional semantic lookup hint. For example, a Spack `spec` tells a client how
to find and activate an existing installation; it is not an instruction to
build or install software.

Execution profiles use only two portable kinds:

- `batch` completes and normally uses `process_exit` readiness.
- `service` remains live and uses either a package progress event or a durable
  `service_runtime` health observation.

Configuration conditions support `equals`, `greater_than`, `is_empty`, and
`is_not_empty`. Every profile and rule is explicit; clients do not need to
interpret help prose to discover requirements.

## Package implementation

Override `_deployment_contract()` and return a
`jarvis_cd.deployment.PackageDeploymentContract`. The base implementation
returns `None`. `Pkg.describe_deployment()` validates the return type and emits
the stable dictionary form.

Runtime facts remain package-owned. A package decides what software and
capabilities it requires, which non-mutating probe establishes usability, what
provider query can resolve it, and which lifecycle signal constitutes
readiness. Contract output must never contain a resolved executable path,
install prefix, or package source path. Provider selectors are semantic values
such as a Spack spec.

Generic implementation and administrative menu settings are marked
`agent_visible: false`. They remain available to the CLI and persisted config,
but an agent-facing describer should omit them. Package semantic inputs remain
visible and are governed by `configuration_rules`.

## Built-in packages

`builtin.lammps` exposes two batch profiles. An empty `script` selects its
bounded generated Lennard-Jones workload; a non-empty `script` selects a user
input. The default trajectory interval is 100, so default configuration always
launches a real finite workload. Host execution resolves LAMMPS from the
activated environment, and the contract offers the Spack spec `lammps` when
that runtime is not yet usable.

`builtin.paraview` exposes batch-script, client-server, and health-checked live
dataset service profiles. The package resolves and capability-checks ParaView
from the active environment, an explicit `PARAVIEW_HOME`, or deterministic
versioned user installations. The selected location is used internally for
launch but is absent from the deployment document. Service mode requires a
headless-capable Python runtime and reports readiness through the durable
service-runtime record.
