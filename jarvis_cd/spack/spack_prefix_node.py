from jarvis_cd.shell.exec_node import ExecNode

class SpackPrefixNode(ExecNode):
    def __init__(self, spack_query, **kwargs):
        cmd = f"spack find  --format \"{{prefix}}\" {spack_query}"
        kwargs['shell'] = True
        super().__init__(cmd, **kwargs)

    def GetPrefix(self):
        return self.GetLocalStdout()[0]
