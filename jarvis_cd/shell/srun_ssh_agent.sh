#!/bin/sh
# An ssh-compatible remote agent that starts the command on the named host
# through Slurm. Open MPI's PRRTE/ORTE calls it as ssh:
#   <agent> [ssh options] host command...
# Inside an allocation Slurm authenticates the launch itself, so an Open MPI
# built without a Slurm launcher needs no node-to-node ssh host key or key.
while [ $# -gt 0 ]; do
  case "$1" in
    -[bcDEeFIiJLlmOopQRSWw]) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
host=$1
shift
overlap=
if srun --help 2>/dev/null | grep -q -- '--overlap'; then
  overlap=--overlap
fi
# A one-task step that binds would pin every rank the daemon starts.
bind=
if scontrol show config 2>/dev/null | grep -Eq '^TaskPlugin .*(affinity|cgroup)'; then
  bind=--cpu-bind=none
fi
# Like ssh, the joined words run in the person's login shell.
exec srun --quiet --nodes=1 --ntasks=1 $overlap $bind --mpi=none \
  --kill-on-bad-exit=1 --nodelist="$host" "${SHELL:-/bin/sh}" -c "$*"
