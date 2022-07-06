
from jarvis_cd.comm.ssh_node import SSHNode

class LinkSpackage(SSHNode):
    def __init__(self, hosts, spack_query_dict, link_path,
                 username, pkey=None, password=None, port=22,
                 sudo=False, print_output=True, collect_output=True, do_ssh=True):
        self.spack_query_dict = spack_query_dict
        package_name = self.spack_query_dict['package_name']
        spack_query = package_name
        cmds = f"ln -s `spack find  --format \"{{prefix}}\" {spack_query}` {link_path}"
        super().__init__(hosts, cmds, username, pkey, password, port, sudo, print_output, collect_output, do_ssh)