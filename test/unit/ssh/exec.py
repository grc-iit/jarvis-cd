from jarvis_cd.ssh.openssh.ssh_exec_node import SSHExecNode
cmds = ['cd /home/cc/jarvis-cd', 'echo $PWD']
SSHExecNode(cmds, hosts=['129.114.108.68']).Run()