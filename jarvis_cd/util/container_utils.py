"""Shared helpers for routing package commands into the pipeline's running
container instance.

jarvis-cd wraps a command as ``apptainer exec ... instance://<name> bash -c
'<cmd>'`` only when the ExecInfo passed to Exec carries a container engine
(``ExecFactory._prepare_container`` gates on ``exec_info.container``). The
framework never injects this automatically: every package must thread the
kwargs into each ExecInfo it builds.

These helpers are the shared implementation of that pattern for any package
(builtin or from an external registered repo) that must behave correctly in
both bare-metal and containerized pipelines: :func:`container_kwargs` routes
Execs into the deploy context, :func:`eff_hostfile` +
:func:`single_instance_menu_opt` implement head-node-only pinning, and
:func:`flatten_stdout` / :func:`all_hosts_ok` normalize multi-host Exec
results.
"""
from jarvis_cd.util.hostfile import Hostfile


def eff_hostfile(pkg, hostfile=None):
    """The hostfile a *head-node-only* service should actually run on.

    Some packages in a multi-node pipeline are single-client baselines or
    metadata singletons, NOT distributed workloads: redis (e.g. as JuiceFS's
    metadata store), the JuiceFS format+FUSE mount, and single-client ior
    baselines are all meant to run on ONE host even though the pipeline hands
    every package the full N-node hostfile. Left unpinned they fan out over
    Pssh to all N nodes and (a) race on shared filesystems (concurrent
    ``rm -rf``/``mkdir`` of a data dir -> "Directory not empty" and a
    half-torn state that breaks the next sweep combo), and (b) on the
    non-head nodes hit services that aren't there ("connection refused") and
    leave broken FUSE mounts behind.

    When this package sets ``single_instance`` and the (effective) hostfile
    has >1 host, collapse to just the FIRST host; otherwise return the
    hostfile unchanged (single-node pipelines and genuinely-distributed
    packages are unaffected).

    :param pkg: the jarvis package instance (``self``)
    :param hostfile: optional base hostfile overriding ``pkg.hostfile`` —
        for packages that pre-slice their hostfile (e.g. ior's ``num_nodes``
        subset) before the single_instance collapse
    :return: a Hostfile (possibly sliced to its first host)
    """
    hf = hostfile if hostfile is not None else pkg.hostfile
    if pkg.config.get('single_instance') and hf is not None \
            and len(hf.hosts) > 1:
        return Hostfile(hosts=hf.hosts[:1],
                        hosts_ip=hf.hosts_ip[:1] if hf.hosts_ip else None)
    return hf


def single_instance_menu_opt(msg=None):
    """The ``single_instance`` config menu entry shared by every
    head-node-only service. Off by default so single-node pipelines and
    distributed workloads are unchanged; set true in a multi-node YAML to pin
    the service to the first host. See :func:`eff_hostfile`.

    :param msg: optional package-specific help text overriding the generic
        message (semantics must stay those of :func:`eff_hostfile`)
    :return: dict (a _configure_menu entry)
    """
    return {
        'name': 'single_instance',
        'msg': msg or (
            'Pin this service to the FIRST host even when the pipeline '
            'hostfile has >1 host (skip fanning out over Pssh). Use for '
            'head-node-only baselines/singletons (e.g. a JuiceFS mount or '
            'a single-client ior baseline) so they do not race on shared '
            'filesystems or hit a service that only runs on the head node. '
            'Default keeps the legacy run-on-every-host behaviour.'),
        'type': bool,
        'default': False,
    }


def container_kwargs(pkg):
    """ExecInfo kwargs that make jarvis's ``Exec._prepare_container`` wrap the
    command to run inside the pipeline's container instance.

    ``pkg._container_engine`` returns the pipeline's engine (e.g.
    ``apptainer``) only when this package's ``deploy_mode`` is ``container``
    (propagated from the pipeline's ``base_deploy_mode``); otherwise it is
    ``'none'`` and ``_prepare_container`` no-ops, so the result is always
    safe to splat into any ExecInfo.

    ``pkg.deploy_image_name()`` is the pipeline RUN name (e.g.
    ``single_node_run1``) — the apptainer instance name — NOT the SIF
    basename from the YAML's ``container_image``.

    :param pkg: the jarvis package instance (``self``)
    :return: dict of ExecInfo kwargs
    """
    return {
        'container': pkg._container_engine,
        'container_image': pkg.deploy_image_name(),
    }


def flatten_stdout(exec_result):
    """Return an Exec result's stdout as one string.

    Pssh/Local Exec results carry stdout either as ``{host: str}`` or as a
    plain string depending on the exec type.

    :param exec_result: the object returned by ``Exec(...).run()``
    :return: stdout joined across hosts
    """
    out = getattr(exec_result, 'stdout', '')
    if isinstance(out, dict):
        return '\n'.join(str(v) for v in out.values())
    return str(out)


def all_hosts_ok(exec_result, expect=None):
    """True iff every host exited 0 and, when ``expect`` is given, every
    host's stdout contains it. Handles dict-or-scalar exit_code/stdout.

    :param exec_result: the object returned by ``Exec(...).run()``
    :param expect: optional substring required in each host's stdout
    :return: bool
    """
    codes = getattr(exec_result, 'exit_code', 1)
    if isinstance(codes, dict):
        if any(c != 0 for c in codes.values()):
            return False
    elif codes != 0:
        return False
    if expect is None:
        return True
    out = getattr(exec_result, 'stdout', '')
    if isinstance(out, dict):
        return all(expect in str(v) for v in out.values())
    return expect in str(out)
