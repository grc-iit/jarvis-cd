"""
Collect the JARVIS resource graph on the nodes of a submitted job.

Submit it with ``jarvis ppl submit`` so the collector runs on the compute
nodes of the allocation, never on a login node. With ``net_only`` (the
default) it records only the ``net`` section that drives the MPI launch
network and keeps the stored storage view.
"""
from jarvis_cd.core.pkg import Application


class ResourceGraph(Application):
    """
    Build the resource graph (``jarvis rg build``) on this package's hosts.
    """
    def _init(self):
        pass

    def component_label(self):
        """Name shown for this component in plans and job cards."""
        if self.config.get('net_only', True):
            return 'resource graph collection (network)'
        return 'resource graph collection'

    def _configure_menu(self):
        return [
            {
                'name': 'net_only',
                'msg': 'Collect only the net section (interfaces); keep storage',
                'type': bool,
                'default': True,
            },
            {
                'name': 'benchmark',
                'msg': 'Benchmark storage (ignored with net_only)',
                'type': bool,
                'default': False,
            },
        ]

    def _configure(self, **kwargs):
        pass

    def start(self):
        from jarvis_cd.core.resource_graph import ResourceGraphManager

        hosts = list(self.get_hostfile().hosts)
        ResourceGraphManager().build(
            benchmark=bool(self.config.get('benchmark', False)),
            net_only=bool(self.config.get('net_only', True)),
            hosts=hosts,
        )

    def stop(self):
        pass

    def clean(self):
        pass
