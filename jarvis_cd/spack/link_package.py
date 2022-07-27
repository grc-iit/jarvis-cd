
from jarvis_cd.shell.exec_node import ExecNode

class LinkSpackage(ExecNode):
    def __init__(self, spack_query_dict, link_path, **kwargs):
        self.spack_query_dict = spack_query_dict
        package_name = self.spack_query_dict['package_name']
        spack_query = package_name
        cmd = f"ln -s `spack find  --format \"{{prefix}}\" {spack_query}` {link_path}"
        kwargs['shell'] = True
        super().__init__(cmd, **kwargs)