#!/bin/bash
# Nyx + IOWarp baremetal sweep — single compute node.
#
# Companion to ~/nyx_sweep_bm.sh, but routes Nyx through jarvis Pipeline B
# (writes to /lus/flare/.../wrp_cte_mount/...) instead of plain Lustre.
# Pipeline A (iowarp_fuse_standalone) MUST be deployed first via:
#   jarvis ppl load yaml ~/jarvis-cd/builtin/pipelines/portability/aurora/iowarp_fuse_standalone.yaml
#   jarvis ppl start
# and stays up for the duration of the sweep.
#
# Usage:
#   source ~/jarvis-cd/builtin/pipelines/portability/aurora/nyx_iowarp_sweep_bm.sh
#   nyx_iowarp_sweep_bm                   # default sweep
#   nyx_iowarp_run 256 4 6                # one-off: n_cell=256^3, max_step=4, 6 ranks
#   nyx_iowarp_sweep_compare              # IOWarp vs plain Lustre side-by-side
#
# Output: CSV at ~/nyx_iowarp_sweep_<TS>.csv with columns
#   target,n_cell,max_step,nprocs,
#   wall_s,run_time_s,chk_total_s,n_chk,plt_total_s,n_plt,
#   bytes_chk,bytes_plt,mb_per_s,failed,
#   gpu_util_max,gpu_util_avg,gpu_mem_peak_mib,gpu_power_max_w
#
# GPU metrics come from xpu-smi (sampled every 1s in background during
# the run). All zeros if xpu-smi is unavailable on the node.

# ---- Configuration (edit if needed) ----------------------------------
TEMPLATE_IOWARP=~/jarvis-cd/builtin/pipelines/portability/aurora/nyx_writes_to_mount.yaml
TEMPLATE_LUSTRE=~/jarvis-cd/builtin/pipelines/portability/aurora/nyx_baremetal_1n.yaml
INPUTS_IOWARP=/lus/flare/projects/gpu_hack/isamuradli/inputs.regtest.sedov.iowarp
MOUNT=/lus/flare/projects/gpu_hack/isamuradli/wrp_cte_mount
LUSTRE_OUT_BASE=/lus/flare/projects/gpu_hack/isamuradli/baremetal_nyx_sweep_lustre

NCELLS_DEFAULT=(64 128 256)
MAX_STEP_DEFAULT=4
NPROCS_DEFAULT_LIST=(1 6)

# ---- helpers ---------------------------------------------------------

# Check Pipeline A foundation is alive
_iowarp_check_alive() {
    pgrep -fa chimaera | grep -v ssh | grep -q "chimaera runtime start" || {
        echo "ERROR: chimaera daemon not running. Deploy Pipeline A first:" >&2
        echo "  jarvis ppl load yaml $(dirname $TEMPLATE_IOWARP)/iowarp_fuse_standalone.yaml" >&2
        echo "  jarvis ppl start" >&2
        return 1
    }
    pgrep -fa wrp_cte_fuse >/dev/null || {
        echo "ERROR: wrp_cte_fuse daemon not running." >&2
        return 1
    }
    mount | grep -q "$MOUNT" || {
        echo "ERROR: $MOUNT is not mounted." >&2
        return 1
    }
    return 0
}

# Generate a customized Nyx-only YAML for one run
# args: template, name, nprocs, ppn, max_step, n_cell, out_path
_gen_yaml() {
    local TEMPLATE=$1 NAME=$2 NPROCS=$3 PPN=$4 STEP=$5 CELL=$6 OUT=$7
    local YAML=/tmp/${NAME}.yaml
    sed -e "s|^name:.*|name: $NAME|" \
        -e "s|nprocs:[ ]*[0-9]*|nprocs: $NPROCS|" \
        -e "s|ppn:[ ]*[0-9]*|ppn: $PPN|" \
        -e "s|max_step:[ ]*[0-9]*|max_step: $STEP|" \
        -e "s|n_cell:[ ]*\"[0-9 ]*\"|n_cell: \"$CELL $CELL $CELL\"|" \
        -e "s|^[ ]*out:.*|    out: $OUT|" \
        "$TEMPLATE" > "$YAML"
    echo "$YAML"
}

# ---- GPU monitoring (xpu-smi) ---------------------------------------
# Aurora ships xpu-smi for Intel Data Center GPU Max metrics. We sample
# every second during each Nyx run and aggregate to peak/avg numbers.

# Start xpu-smi in background; echoes PID (empty if xpu-smi unavailable).
# Metric IDs we sample:
#   0  = GPU Utilization (%)
#   5  = GPU Memory Used (MiB)
#   18 = GPU Power (W)
_xpu_start() {
    local XPU_LOG=$1
    if ! command -v xpu-smi >/dev/null 2>&1 && ! module load xpu-smi 2>/dev/null; then
        return 0
    fi
    nohup xpu-smi dump -d -1 -m 0,5,18 -i 1 > "$XPU_LOG" 2>&1 &
    echo $!
}

_xpu_stop() {
    local XPID=$1
    [ -z "$XPID" ] && return 0
    # Fully non-blocking: just signal and continue. xpu-smi was launched
    # via `nohup ... &` so it's already detached. Samples written to its
    # log during the Nyx run are what we'll parse in _xpu_parse — no
    # need to wait for the process to actually exit before continuing.
    kill "$XPID" 2>/dev/null
}

# Parse xpu-smi dump and emit "util_max,util_avg,mem_peak_mb,power_max_w".
# xpu-smi columns are typically: Timestamp, DeviceId, util%, mem_MiB, power_W
# Header line is skipped; lines may include warnings. Tolerate both formats.
_xpu_parse() {
    local XPU_LOG=$1
    if [ ! -s "$XPU_LOG" ]; then
        echo "0,0,0,0"
        return
    fi
    awk -F'[, \t]+' '
    NR == 1 { next }                                  # skip header
    /^[0-9]/ {                                        # rows starting with timestamp
        # Find numeric metric columns. Format is roughly:
        # "YYYY-MM-DDTHH:MM:SS.mmm, dev, util, mem, power"
        # Different xpu-smi versions emit slightly different layouts; we
        # take the LAST 3 numeric fields per row as util, mem, power.
        n = NF
        if (n < 4) next
        u = $(n-2) + 0
        m = $(n-1) + 0
        p = $n + 0
        if (u > util_max) util_max = u
        util_sum += u; util_n++
        if (m > mem_max) mem_max = m
        if (p > pwr_max) pwr_max = p
    }
    END {
        ua = util_n ? util_sum / util_n : 0
        printf "%.1f,%.1f,%.0f,%.1f", util_max+0, ua+0, mem_max+0, pwr_max+0
    }
    ' "$XPU_LOG"
}

# Convert bytes to a short human-readable string (e.g. 22.7M, 1.3G)
_bytes_h() {
    local B=${1:-0}
    awk -v b="$B" 'BEGIN{
        split("B K M G T", u);
        for(i=1; b>=1024 && i<5; i++) b/=1024;
        printf (i==1?"%d%s":"%.1f%s"), b, u[i]
    }'
}

# Parse a Nyx run log and emit one CSV row
# args: target_label, n_cell, max_step, nprocs, wall_s, log_file, out_dir, xpu_log
_parse_log() {
    local TARGET=$1 NCELL=$2 STEP=$3 NPROCS=$4 WALL=$5 LOG=$6 OUT=$7 XPU_LOG=$8
    local RUN_T=$(grep "Run time =" "$LOG" | awk '{print $4}')
    local CHK_T=$(grep "checkPoint() time" "$LOG" | awk '{s+=$4}END{print s+0}')
    local PLT_T=$(grep "Write plotfile time" "$LOG" | awk '{s+=$5}END{print s+0}')
    local NCHK=$(grep -c "^checkPoint() time" "$LOG")
    local NPLT=$(grep -c "Write plotfile time" "$LOG")
    local BYTES_CHK=0 BYTES_PLT=0
    if [ -d "$OUT" ]; then
        BYTES_CHK=$(du -sb "$OUT"/chk* 2>/dev/null | awk '{s+=$1}END{print s+0}')
        BYTES_PLT=$(du -sb "$OUT"/plt* 2>/dev/null | awk '{s+=$1}END{print s+0}')
    fi
    local TOTAL_BYTES=$((BYTES_CHK + BYTES_PLT))
    local IO_T=$(echo "$CHK_T + $PLT_T" | bc -l 2>/dev/null)
    local MBPS=0
    if [ -n "$IO_T" ] && [ "$IO_T" != "0" ]; then
        MBPS=$(echo "scale=1; $TOTAL_BYTES / 1048576 / $IO_T" | bc -l 2>/dev/null)
    fi
    # Count any indication that Nyx didn't complete cleanly. We OR many
    # patterns because failure modes vary: chimaera RPC drop, AMReX abort,
    # SYCL/GPU OOM, MPI abort, missing Run time line, etc.
    local FAILED=$(grep -cE "Transport endpoint is not connected|amrex::Error|amrex::Abort|MPI_Abort|Out of memory|sycl::aligned_alloc_device returned nullptr|SIGABRT" "$LOG")
    # Also flag as failed if Nyx never reported a Run time at all.
    if [ -z "$RUN_T" ] && [ "$FAILED" = "0" ]; then
        FAILED=1
    fi
    # GPU metrics from xpu-smi (zeros if unavailable / no log)
    local XPU="0,0,0,0"
    [ -n "$XPU_LOG" ] && [ -s "$XPU_LOG" ] && XPU=$(_xpu_parse "$XPU_LOG")
    echo "$TARGET,$NCELL,$STEP,$NPROCS,$WALL,${RUN_T:-NA},${CHK_T:-0},${NCHK:-0},${PLT_T:-0},${NPLT:-0},$BYTES_CHK,$BYTES_PLT,${MBPS:-0},$FAILED,$XPU"
}

# Color helpers (no-op if NO_COLOR is set or stdout isn't a tty)
_c_off() { tput sgr0 2>/dev/null || printf ''; }
_c_dim() { tput dim 2>/dev/null || printf ''; }
_c_bold() { tput bold 2>/dev/null || printf ''; }
_c_red() { tput setaf 1 2>/dev/null || printf ''; }
_c_green() { tput setaf 2 2>/dev/null || printf ''; }
_c_yellow() { tput setaf 3 2>/dev/null || printf ''; }
_c_cyan() { tput setaf 6 2>/dev/null || printf ''; }
if [ -n "$NO_COLOR" ] || [ ! -t 1 ]; then
    _c_off()  { :; }; _c_dim()  { :; }; _c_bold() { :; }
    _c_red()  { :; }; _c_green(){ :; }; _c_yellow(){ :; }; _c_cyan(){ :; }
fi

# Pretty-print column header — keep this in sync with _pretty_row.
# Layout: target n_cell step ranks | wall  nyx | chk_t #chk plt_t #plt | size  MB/s | OK
_pretty_header() {
    printf '%s' "$(_c_bold)$(_c_cyan)"
    printf '%-7s  %-7s  %-6s %-4s %-5s |  %7s  %7s |  %7s %4s  %7s %4s |  %8s  %8s |  %6s  %6s  %7s  %6s | %s\n' \
        "run" "target" "n_cell" "step" "ranks" \
        "wall_s" "nyx_s" \
        "chk_s" "#chk" "plt_s" "#plt" \
        "size" "MB/s" \
        "g_max%" "g_avg%" "g_mem_M" "g_pwrW" \
        "OK"
    printf '%s' "$(_c_off)"
}

# Pretty-print one row from a CSV line.
# Optional first arg: run label (e.g. "1/6", or "" for standalone).
_pretty_row() {
    local RUN_LABEL=""
    if [ "$#" -ge 2 ]; then
        RUN_LABEL=$1; shift
    fi
    # CSV columns (must match _parse_log output and CSV header):
    #   target n_cell max_step nprocs wall run_t chk_t n_chk plt_t n_plt
    #   bytes_chk bytes_plt mbps failed gpu_util_max gpu_util_avg gpu_mem_mib gpu_power_w
    IFS=',' read -r TGT NC ST NP WALL RUN CHK NCK PLT NPL BCHK BPLT MBPS FAIL \
                    GUMAX GUAVG GMEM GPWR <<< "$1"
    local SIZE=$(_bytes_h $((BCHK + BPLT)))
    local STATUS_COLOR="$(_c_green)" STATUS="OK"
    if [ "${FAIL:-0}" != "0" ]; then
        STATUS_COLOR="$(_c_red)"; STATUS="FAIL($FAIL)"
    fi
    local TARGET_COLOR="$(_c_cyan)"
    [ "$TGT" = "lustre" ] && TARGET_COLOR="$(_c_yellow)"

    # Convert RUN ("4.417047024") -> 4.417 when numeric, else NA.
    local RUN_FMT="    NA "
    if [[ "$RUN" =~ ^[0-9.]+$ ]]; then
        RUN_FMT=$(printf '%7.3f' "$RUN")
    fi

    # Append units to GPU metrics so columns are self-documenting:
    #   util fields → "%", memory → "M" (MiB), power → "W".
    local GUMAX_FMT="${GUMAX:-0}%"
    local GUAVG_FMT="${GUAVG:-0}%"
    local GMEM_FMT="${GMEM:-0}M"
    local GPWR_FMT="${GPWR:-0}W"

    printf '%s%-7s%s  %s%-7s%s  %-6s %-4s %-5s |  %7.2f  %s |  %7.3f %4s  %7.3f %4s |  %8s  %8s |  %6s  %6s  %7s  %6s | %s%s%s\n' \
        "$(_c_dim)" "${RUN_LABEL:--}" "$(_c_off)" \
        "$TARGET_COLOR" "$TGT" "$(_c_off)" \
        "${NC}^3" "$ST" "$NP" \
        "${WALL:-0}" "$RUN_FMT" \
        "${CHK:-0}" "$NCK" "${PLT:-0}" "$NPL" \
        "$SIZE" "${MBPS:-0}" \
        "$GUMAX_FMT" "$GUAVG_FMT" "$GMEM_FMT" "$GPWR_FMT" \
        "$STATUS_COLOR" "$STATUS" "$(_c_off)"
}

# ---- single run ------------------------------------------------------

# Run Nyx once through the IOWarp Pipeline B path.
# args: n_cell, max_step (default 4), nprocs (default 1)
nyx_iowarp_run() {
    local NCELL=${1:?usage: nyx_iowarp_run <n_cell> [max_step] [nprocs]}
    local STEP=${2:-$MAX_STEP_DEFAULT}
    local NPROCS=${3:-1}
    local PPN=${PPN:-$NPROCS}
    local NAME="nyx_iow_n${NCELL}_s${STEP}_p${NPROCS}"
    local OUT="$MOUNT/${NAME}_out"
    local LOG=/tmp/${NAME}.log

    _iowarp_check_alive || return 1

    # Clear any previous output for this run from the mount
    rm -rf "$OUT" 2>/dev/null

    local YAML=$(_gen_yaml "$TEMPLATE_IOWARP" "$NAME" "$NPROCS" "$PPN" "$STEP" "$NCELL" "$OUT")
    local XPU_LOG=/tmp/${NAME}.xpu.log

    local XPID=$(_xpu_start "$XPU_LOG")
    local START=$(date +%s.%N)
    # Only destroy a previously-loaded Pipeline B with the SAME name.
    # NEVER call bare `jarvis ppl destroy` — that destroys whatever is
    # current, which after `jarvis ppl start` of Pipeline A is Pipeline
    # A itself (unmounting FUSE and killing chimaera). load yaml is
    # idempotent: it replaces the current pipeline state without
    # touching foreign daemons.
    if jarvis ppl list 2>/dev/null | grep -q "^${NAME}\$"; then
        jarvis ppl cd "$NAME" >/dev/null 2>&1 && jarvis ppl destroy 2>/dev/null
    fi
    jarvis ppl load yaml "$YAML" > /dev/null 2>&1
    jarvis ppl run > "$LOG" 2>&1
    local END=$(date +%s.%N)
    _xpu_stop "$XPID"
    local WALL=$(echo "$END - $START" | bc -l)

    local ROW=$(_parse_log "iowarp" "$NCELL" "$STEP" "$NPROCS" "$WALL" "$LOG" "$OUT" "$XPU_LOG")
    # Pretty print to stderr only when called as a standalone (not from a sweep
    # that already prints its own header). Detect via env var _NYX_SWEEP_RUNNING.
    if [ -z "$_NYX_SWEEP_RUNNING" ]; then
        _pretty_header >&2
        _pretty_row "$ROW" >&2
    fi
    echo "$ROW"
}

# Run Nyx once writing to PLAIN LUSTRE (no IOWarp). For comparison.
# args: n_cell, max_step, nprocs
nyx_lustre_run() {
    local NCELL=${1:?usage: nyx_lustre_run <n_cell> [max_step] [nprocs]}
    local STEP=${2:-$MAX_STEP_DEFAULT}
    local NPROCS=${3:-1}
    local PPN=${PPN:-$NPROCS}
    local NAME="nyx_lus_n${NCELL}_s${STEP}_p${NPROCS}"
    local OUT="${LUSTRE_OUT_BASE}_${NAME}"
    local LOG=/tmp/${NAME}.log

    rm -rf "$OUT" 2>/dev/null
    mkdir -p "$OUT"

    local YAML=$(_gen_yaml "$TEMPLATE_LUSTRE" "$NAME" "$NPROCS" "$PPN" "$STEP" "$NCELL" "$OUT")
    local XPU_LOG=/tmp/${NAME}.xpu.log

    local XPID=$(_xpu_start "$XPU_LOG")
    local START=$(date +%s.%N)
    # Same Pipeline-A-protection as nyx_iowarp_run above.
    if jarvis ppl list 2>/dev/null | grep -q "^${NAME}\$"; then
        jarvis ppl cd "$NAME" >/dev/null 2>&1 && jarvis ppl destroy 2>/dev/null
    fi
    jarvis ppl load yaml "$YAML" > /dev/null 2>&1
    jarvis ppl run > "$LOG" 2>&1
    local END=$(date +%s.%N)
    _xpu_stop "$XPID"
    local WALL=$(echo "$END - $START" | bc -l)

    local ROW=$(_parse_log "lustre" "$NCELL" "$STEP" "$NPROCS" "$WALL" "$LOG" "$OUT" "$XPU_LOG")
    if [ -z "$_NYX_SWEEP_RUNNING" ]; then
        _pretty_header >&2
        _pretty_row "$ROW" >&2
    fi
    echo "$ROW"
}

# ---- sweeps ----------------------------------------------------------

# Default IOWarp-only sweep: cross-product of NCELLS_DEFAULT and NPROCS_DEFAULT_LIST.
nyx_iowarp_sweep_bm() {
    _iowarp_check_alive || return 1
    local TS=$(date +%Y%m%d_%H%M%S)
    local CSV=~/nyx_iowarp_sweep_${TS}.csv
    echo "target,n_cell,max_step,nprocs,wall_s,run_time_s,chk_total_s,n_chk,plt_total_s,n_plt,bytes_chk,bytes_plt,mb_per_s,failed,gpu_util_max,gpu_util_avg,gpu_mem_peak_mib,gpu_power_max_w" > "$CSV"

    local TOTAL=$((${#NCELLS_DEFAULT[@]} * ${#NPROCS_DEFAULT_LIST[@]}))
    printf '%s\n' "$(_c_bold)=== nyx_iowarp_sweep_bm ($TOTAL runs) ===$(_c_off)"
    printf '  output csv: %s\n' "$CSV"
    printf '  prereq:     Pipeline A (chimaera + wrp_cte_fuse) up — verified\n\n'
    _pretty_header
    export _NYX_SWEEP_RUNNING=1
    # Ensure leak-protection: if the sweep is interrupted (Ctrl-C) or any
    # subshell exits early, unset the flag so subsequent standalone
    # nyx_iowarp_run calls still print their own pretty header.
    trap 'unset _NYX_SWEEP_RUNNING; trap - INT TERM RETURN' INT TERM RETURN

    local i=0
    for NCELL in "${NCELLS_DEFAULT[@]}"; do
        for NPROCS in "${NPROCS_DEFAULT_LIST[@]}"; do
            i=$((i+1))
            local CSV_ROW=$(nyx_iowarp_run "$NCELL" "$MAX_STEP_DEFAULT" "$NPROCS" 2>/dev/null | tail -1)
            echo "$CSV_ROW" >> "$CSV"
            _pretty_row "$i/$TOTAL" "$CSV_ROW"
        done
    done

    unset _NYX_SWEEP_RUNNING
    printf '\n%s=== sweep complete ===%s\n' "$(_c_bold)" "$(_c_off)"
    printf '  csv:        %s\n' "$CSV"
    printf '  rows:       %d\n' "$((i))"
}

# Side-by-side IOWarp vs plain Lustre comparison.
nyx_iowarp_sweep_compare() {
    _iowarp_check_alive || return 1
    local TS=$(date +%Y%m%d_%H%M%S)
    local CSV=~/nyx_iowarp_compare_${TS}.csv
    echo "target,n_cell,max_step,nprocs,wall_s,run_time_s,chk_total_s,n_chk,plt_total_s,n_plt,bytes_chk,bytes_plt,mb_per_s,failed,gpu_util_max,gpu_util_avg,gpu_mem_peak_mib,gpu_power_max_w" > "$CSV"

    local TOTAL=$((${#NCELLS_DEFAULT[@]} * ${#NPROCS_DEFAULT_LIST[@]} * 2))
    printf '%s\n' "$(_c_bold)=== nyx_iowarp_sweep_compare ($TOTAL runs: iowarp vs lustre) ===$(_c_off)"
    printf '  output csv: %s\n\n' "$CSV"
    _pretty_header
    export _NYX_SWEEP_RUNNING=1
    # Ensure leak-protection: if the sweep is interrupted (Ctrl-C) or any
    # subshell exits early, unset the flag so subsequent standalone
    # nyx_iowarp_run calls still print their own pretty header.
    trap 'unset _NYX_SWEEP_RUNNING; trap - INT TERM RETURN' INT TERM RETURN

    local i=0
    for NCELL in "${NCELLS_DEFAULT[@]}"; do
        for NPROCS in "${NPROCS_DEFAULT_LIST[@]}"; do
            for FN in nyx_iowarp_run nyx_lustre_run; do
                i=$((i+1))
                local CSV_ROW=$($FN "$NCELL" "$MAX_STEP_DEFAULT" "$NPROCS" 2>/dev/null | tail -1)
                echo "$CSV_ROW" >> "$CSV"
                _pretty_row "$i/$TOTAL" "$CSV_ROW"
            done
        done
    done

    unset _NYX_SWEEP_RUNNING
    printf '\n%s=== compare complete ===%s\n' "$(_c_bold)" "$(_c_off)"
    printf '  csv:        %s\n' "$CSV"
    printf '  rows:       %d\n' "$((i))"
}

export -f _iowarp_check_alive _gen_yaml _parse_log
export -f nyx_iowarp_run nyx_lustre_run
export -f nyx_iowarp_sweep_bm nyx_iowarp_sweep_compare

echo "Loaded nyx_iowarp_sweep_bm. Functions:"
echo "  nyx_iowarp_run <n_cell> [max_step] [nprocs]"
echo "  nyx_lustre_run <n_cell> [max_step] [nprocs]"
echo "  nyx_iowarp_sweep_bm                  # IOWarp-only matrix sweep"
echo "  nyx_iowarp_sweep_compare             # IOWarp vs plain Lustre side-by-side"
echo
echo "Prereq: Pipeline A (iowarp_fuse_standalone) must be deployed via 'jarvis ppl start'."
