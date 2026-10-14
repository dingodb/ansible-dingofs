#!/usr/bin/env bash
# cpuset-detect.sh - machine-parseable free-CPU detection for ansible dingo_cache role.
# (Twin: roles/dingo_client/files/cpuset-detect.sh is the same script with the
# same flags. Keep the two in sync; the roles deploy to disjoint host groups
# and each must stand alone.)
# Claim logic and the least-busy fallback follow the manual cpuset-check.sh
# triage script (kernel isolation, cgroup cpusets, kubelet exclusive CPUs,
# process --cpuset=/affinity, 1s /proc/stat usage sample — no sysstat needed,
# NUMA topology), recast into suggest/check modes whose stdout the role parses
# into --cpuset. All human diagnostics go to stderr.
#
# Usage: cpuset-detect.sh [-s SHARDS] [-n NUMA_NODE] [-c CPUSET] [-b BUSY_PCT]
#                         [--siblings] [-x EXCLUDE] [-S SELF_COMM] [--no-busy]
#   suggest mode (default): print SUGGEST=<range|NONE> (+ FREE/CORES/NODE detail lines),
#                           always followed by FORCED=<range|NONE> + FORCED_NODE=<node>
#                           + FORCED_USAGE=<cpu:usage%,...> (one decimal, display only).
#                           FORCED is the LEAST-BUSY pick ignoring all discovered
#                           claims (candidates ranked by sampled usage, lowest
#                           first, ties by CPU id) — last resort for hosts with
#                           zero free CPUs; explicit -x exclusions are still
#                           honoured. Exit 2 when SUGGEST is NONE, 0 otherwise.
#   check mode (-c):        exit 0 when every CPU in CPUSET is free+online, else exit 1
#                           and print one CONFLICT <cpu> <reasons> line per bad CPU (stdout),
#                           plus a NODES=<spanned NUMA nodes> line in both cases.
#   -x EXCLUDE:   treat these CPUs as claimed (cpusets assigned earlier this run).
#   -S SELF_COMM: skip processes with this comm (own binary) in the process-claim
#                 scan, so a redeploy does not see its own running cpuset as taken.
#   --no-busy:    skip the mpstat busy sample (stability re-validation: our own
#                 poll-mode shards spin at ~100% and must not evict themselves).
export LC_ALL=C
set -u
SHARDS=8; ONLY_NODE=""; CHECK=""; BUSY_PCT=30; SIBLINGS=0
EXCLUDE=""; SELF_NAME=""; NO_BUSY=0
while [ $# -gt 0 ]; do
  case $1 in
    -s) SHARDS=$2; shift 2;;
    -n) ONLY_NODE=$2; shift 2;;
    -c) CHECK=$2; shift 2;;
    -b) BUSY_PCT=$2; shift 2;;
    -x) EXCLUDE=$2; shift 2;;
    -S) SELF_NAME=$2; shift 2;;
    --siblings) SIBLINGS=1; shift;;
    --no-busy) NO_BUSY=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 3;;
  esac
done

expand() {                      # "0-3,8" -> one CPU per line
  local part a b
  for part in ${1//,/ }; do
    case $part in
      *-*) a=${part%-*}; b=${part#*-}; seq "$a" "$b" ;;
      ?*)  echo "$part" ;;
    esac
  done
}
compress() {                    # CPUs on stdin -> "0-3,8"
  sort -n -u | awk '
    NR==1 {s=p=$1; next}
    $1==p+1 {p=$1; next}
    {printf "%s%s", sep, (s==p?s:s"-"p); sep=","; s=p=$1}
    END {if (NR) printf "%s%s", sep, (s==p?s:s"-"p); print ""}'
}
best_run() {                    # sorted ids on stdin -> "start end len" of longest run
  sort -n -u | awk '
    function upd() { if (p-s+1 > bl) {bl=p-s+1; bs=s; be=p} }
    NR==1 {s=p=$1; next}
    $1==p+1 {p=$1; next}
    {upd(); s=p=$1}
    END {if (NR) {upd(); print bs, be, bl}}'
}
read_file() { [ -r "$1" ] && tr -d '\n' < "$1"; }

declare -A REASON NODE_OF SIB
claim() {                       # claim "<cpu list>" "<reason>"
  local c
  for c in $(expand "$1"); do REASON[$c]+="$2; "; done
}

ONLINE=$(read_file /sys/devices/system/cpu/online)
ISOLATED=$(read_file /sys/devices/system/cpu/isolated)
ONLINE_N=$(expand "$ONLINE" | wc -l)

# ---- topology ----
declare -A NODE_CPU_CNT
while IFS=, read -r c n; do
  [ -n "$c" ] || continue
  n=${n:-0}
  NODE_OF[$c]=$n
  NODE_CPU_CNT[$n]=$(( ${NODE_CPU_CNT[$n]:-0} + 1 ))
done < <(lscpu -p=CPU,NODE 2>/dev/null | grep -v '^#')
# Fallback to sysfs when lscpu is absent or too old for -p=CPU,NODE
if [ "${#NODE_OF[@]}" -eq 0 ]; then
  for nf in /sys/devices/system/node/node[0-9]*/cpulist; do
    [ -r "$nf" ] || continue
    n=$(basename "$(dirname "$nf")" | tr -dc '0-9')
    for c in $(expand "$(read_file "$nf")"); do
      NODE_OF[$c]=${n:-0}
      NODE_CPU_CNT[${n:-0}]=$(( ${NODE_CPU_CNT[${n:-0}]:-0} + 1 ))
    done
  done
fi
if [ "${#NODE_OF[@]}" -eq 0 ]; then
  for c in $(expand "$ONLINE"); do
    NODE_OF[$c]=0
    NODE_CPU_CNT[0]=$(( ${NODE_CPU_CNT[0]:-0} + 1 ))
  done
fi
MIN_NODE_CPUS=$ONLINE_N
for n in "${!NODE_CPU_CNT[@]}"; do
  [ "${NODE_CPU_CNT[$n]}" -lt "$MIN_NODE_CPUS" ] && MIN_NODE_CPUS=${NODE_CPU_CNT[$n]}
done
for c in $(expand "$ONLINE"); do
  SIB[$c]=$(read_file /sys/devices/system/cpu/cpu$c/topology/thread_siblings_list)
  : "${SIB[$c]:=$c}"
done

# ---- 1. kernel isolation ----
[ -n "$ISOLATED" ] && claim "$ISOLATED" "kernel-isolated"

# ---- 2. cgroup cpusets (skip root set and shared pools >= MIN_NODE_CPUS) ----
if [ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)" = cgroup2fs ]; then
  ROOT_EFF=$(read_file /sys/fs/cgroup/cpuset.cpus.effective)
  while IFS= read -r f; do
    d=$(dirname "$f"); want=$(read_file "$f"); [ -z "$want" ] && continue
    eff=$(read_file "$d/cpuset.cpus.effective")
    [ -z "$eff" ] || [ "$eff" = "$ROOT_EFF" ] && continue
    [ "$(expand "$eff" | wc -l)" -lt "$MIN_NODE_CPUS" ] || continue
    claim "$eff" "cgroup ${d#/sys/fs/cgroup}"
  done < <(find /sys/fs/cgroup -mindepth 2 -name cpuset.cpus 2>/dev/null)
else
  ROOT_EFF=$(read_file /sys/fs/cgroup/cpuset/cpuset.effective_cpus)
  while IFS= read -r f; do
    d=$(dirname "$f"); eff=$(read_file "$f")
    [ -z "$eff" ] || [ "$eff" = "$ROOT_EFF" ] && continue
    [ "$(expand "$eff" | wc -l)" -lt "$MIN_NODE_CPUS" ] || continue
    claim "$eff" "cgroup ${d#/sys/fs/cgroup/cpuset}"
  done < <(find /sys/fs/cgroup/cpuset -mindepth 2 -name cpuset.effective_cpus 2>/dev/null)
fi

# ---- 3. kubelet static CPU manager (jq optional — skip when absent) ----
STATE=/var/lib/kubelet/cpu_manager_state
if [ -r "$STATE" ] && command -v jq >/dev/null; then
  while read -r l; do [ -n "$l" ] && claim "$l" "kubelet-exclusive"; done \
    < <(jq -r '.entries // {} | .[] | .[]' "$STATE")
fi
for kcfg in /etc/kubernetes/kubelet-conf.yml /var/lib/kubelet/config.yaml; do
  [ -r "$kcfg" ] || continue
  kres=$(awk -F'["'"'"' ]+' '/^[[:space:]]*reservedSystemCPUs:/{print $2; exit}' "$kcfg" 2>/dev/null)
  [ -n "$kres" ] && claim "$kres" "kubelet-reserved"
done

# ---- 4. other processes: --cpuset= in cmdline, or narrow affinity ----
# A service started with --cpuset=32-63 --shards=8 owns all of 32-63.
# Whole-NUMA-node affinity (e.g. numactl --cpunodebind=0 or kernel helpers
# bound to all CPUs of a socket, count >= MIN_NODE_CPUS) is node-level
# scheduling, not core pinning — only narrower masks count as pinned.
SELF=$$
for p in /proc/[0-9]*; do
  pid=${p#/proc/}; [ "$pid" = "$SELF" ] && continue
  cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
  [ -z "$cmd" ] && continue                      # kernel thread
  comm=$(cut -c1-20 "$p/comm" 2>/dev/null)
  # Own binary (e.g. redeploy while the service runs): its cpuset/affinity is
  # ours, not a conflict. Anything else is still claimed as before.
  if [ -n "$SELF_NAME" ] && [ "$comm" = "$SELF_NAME" ]; then continue; fi
  cs=$(grep -oE -- '--cpuset[= ][^ ]+' <<<"$cmd" | head -1 | sed -E 's/--cpuset[= ]//')
  if [ -n "$cs" ]; then
    claim "$cs" "pid $pid $comm --cpuset=$cs"; continue
  fi
  a=$(awk '/Cpus_allowed_list/ {print $2}' "$p/status" 2>/dev/null)
  if [ -n "$a" ] && [ "$a" != "$ONLINE" ] && [ "$(expand "$a" | wc -l)" -lt "$MIN_NODE_CPUS" ]; then
    claim "$a" "pid $pid $comm pinned=$a"
  fi
done

# ---- 5. per-CPU usage (1s sample of /proc/stat; no sysstat needed) ----
# CPUs busier than BUSY_PCT count as claimed. Skipped with --no-busy
# (stability re-validation: our own poll-mode shards spin at ~100% and must
# not evict themselves). USAGE[] also feeds the least-busy FORCED fallback.
declare -A USAGE
if [ "$NO_BUSY" = 0 ]; then
  snap() { awk '/^cpu[0-9]+ /{t=0; for(i=2;i<=9;i++) t+=$i; print substr($1,4), $5+$6, t}' /proc/stat; }
  S1=$(snap); sleep 1; S2=$(snap)
  while read -r c u; do
    USAGE[$c]=$u
    [ "$(awk -v b="$u" -v t="$BUSY_PCT" 'BEGIN{print (b>t)}')" = 1 ] && claim "$c" "busy ${u}%"
  done < <(awk 'NR==FNR {i[$1]=$2; t[$1]=$3; next}
                {dt=$3-t[$1]; di=$2-i[$1]; printf "%s %.1f\n", $1, (dt>0 ? 100*(dt-di)/dt : 0)}' \
           <(echo "$S1") <(echo "$S2"))
else
  echo "note: busy check disabled (--no-busy)" >&2
fi

# ---- 6. exclusions handed in by the role (cpusets assigned earlier this run) ----
[ -n "$EXCLUDE" ] && claim "$EXCLUDE" "assigned-this-run"

is_free() { [ -z "${REASON[$1]:-}" ]; }

# ---- check mode: validate a given cpuset ----
if [ -n "$CHECK" ]; then
  bad=0
  for c in $(expand "$CHECK"); do
    if ! expand "$ONLINE" | grep -qx "$c"; then echo "CONFLICT $c NOT-ONLINE"; bad=1
    elif ! is_free "$c"; then echo "CONFLICT $c ${REASON[$c]}"; bad=1; fi
  done
  nodes=$(for c in $(expand "$CHECK"); do echo "${NODE_OF[$c]:-?}"; done | sort -u | tr '\n' ' ')
  echo "NODES=$nodes"
  [ $bad = 0 ] && echo "OK $CHECK"
  exit $bad
fi

# ---- suggest mode: SHARDS free whole cores on the preferred NUMA node ----
[ -n "$ONLY_NODE" ] && NODES="$ONLY_NODE" || NODES=$(printf '%s\n' "${NODE_OF[@]}" | sort -n -u)
PICK_NODE=""
if [ -n "$ONLY_NODE" ]; then
  PICK_NODE="$ONLY_NODE"
else
  # Prefer the node with the most free whole cores so heterogeneous hosts still converge.
  best=0
  for n in $NODES; do
    cnt=0
    for c in $(expand "$ONLINE"); do
      [ "${NODE_OF[$c]:-?}" = "$n" ] || continue
      sibs=$(expand "${SIB[$c]}" | sort -n)
      [ "$c" = "$(head -1 <<<"$sibs")" ] || continue
      ok=1; for s in $sibs; do is_free "$s" || ok=0; done
      [ $ok = 1 ] && cnt=$((cnt+1))
    done
    [ "$cnt" -gt "$best" ] && { best=$cnt; PICK_NODE=$n; }
  done
  [ -z "$PICK_NODE" ] && PICK_NODE=$(echo "$NODES" | head -1)
fi

free=(); cores=()
for c in $(expand "$ONLINE"); do
  [ "${NODE_OF[$c]:-?}" = "$PICK_NODE" ] || continue
  is_free "$c" || continue
  free+=("$c")
  sibs=$(expand "${SIB[$c]}" | sort -n)
  [ "$c" = "$(head -1 <<<"$sibs")" ] || continue      # first thread of the core only
  ok=1; for s in $sibs; do is_free "$s" || ok=0; done
  [ $ok = 1 ] && cores+=("$c")
done

FREE_STR=$(printf '%s\n' "${free[@]:-}" | compress)
CORE_STR=$(printf '%s\n' "${cores[@]:-}" | compress)
echo "NODE=$PICK_NODE"
echo "FREE=$FREE_STR"
echo "CORES=$CORE_STR"

suggest=""
if [ "${#cores[@]}" -ge "$SHARDS" ]; then
  # Pick from the high end of the NUMA node downward (last SHARDS cores) so
  # host-level dingo services stay at the opposite end from kubelet's static
  # CPU manager, which allocates Guaranteed pods from the lowest core IDs up.
  picked=$(printf '%s\n' "${cores[@]: -$SHARDS}")
  if [ "$SIBLINGS" = 1 ]; then
    out=(); for c in $picked; do out+=($(expand "${SIB[$c]}")); done
    suggest=$(printf '%s\n' "${out[@]}" | compress)
  else
    suggest=$(printf '%s\n' "$picked" | compress)
  fi
fi

if [ -n "$suggest" ]; then
  echo "SUGGEST=$suggest"
else
  echo "SUGGEST=NONE"
  echo "need $SHARDS free whole cores on node $PICK_NODE, have ${#cores[@]} (free CPUs: ${FREE_STR:-none})" >&2
fi

# ---- forced fallback: topology-only pick, ignoring all discovered claims ----
# Last resort for hosts with zero free CPUs (shared CPU boxes): overlap with
# whatever runs there is certain, but the service still starts. Explicit -x
# exclusions (cpusets assigned earlier this run) ARE still honoured so sibling
# mounts never collide. Respects -n when that node has enough non-excluded
# cores, otherwise takes the node with the most.
EXC=" $(expand "$EXCLUDE" | tr '\n' ' ') "
declare -A TOTAL
for c in $(expand "$ONLINE"); do
  [[ "$EXC" == *" $c "* ]] && continue
  sibs=$(expand "${SIB[$c]}" | sort -n)
  [ "$c" = "$(head -1 <<<"$sibs")" ] || continue
  n=${NODE_OF[$c]:-0}
  TOTAL[$n]=$((${TOTAL[$n]:-0}+1))
done

F_NODE=""
if [ -n "$ONLY_NODE" ]; then
  [ "${TOTAL[$ONLY_NODE]:-0}" -ge "$SHARDS" ] && F_NODE=$ONLY_NODE
else
  F_BEST=0
  for n in $NODES; do
    t=${TOTAL[$n]:-0}
    if [ "$t" -ge "$SHARDS" ] && [ "$t" -gt "$F_BEST" ]; then F_BEST=$t; F_NODE=$n; fi
  done
fi

forced=""
forced_usage=""
if [ -n "$F_NODE" ]; then
  # Least-busy pick: rank candidates by sampled 1s /proc/stat usage (lowest
  # first, ties by CPU id) instead of taking the topologically first cores —
  # on a fully claimed box this lands shards on the idlest CPUs available
  # (e.g. pinned-but-0.0% CPUs like 37,39,43,46-49,53) rather than on
  # whatever burns hottest.
  if [ "$SIBLINGS" = 1 ]; then
    # Rank first-thread cores by their hottest thread, expand winners to all threads.
    rank=$(for c in $(expand "$ONLINE"); do
      [ "${NODE_OF[$c]:-?}" = "$F_NODE" ] || continue
      [[ "$EXC" == *" $c "* ]] && continue
      sibs=$(expand "${SIB[$c]}" | sort -n)
      [ "$c" = "$(head -1 <<<"$sibs")" ] || continue
      m=0
      for s in $sibs; do
        u=${USAGE[$s]:-0}
        [ "$(awk -v a="$u" -v b="$m" 'BEGIN{print (a>b)}')" = 1 ] && m=$u
      done
      echo "$m $c"
    done | sort -k1,1n -k2,2nr | head -n "$SHARDS")
    out=()
    while read -r m c; do
      [ -n "$c" ] || continue
      for s in $(expand "${SIB[$c]}"); do
        out+=("$s")
        forced_usage+="$s:${USAGE[$s]:-0},"
      done
    done <<<"$rank"
    forced=$(printf '%s\n' "${out[@]}" | compress)
  else
    rank=$(for c in $(expand "$ONLINE"); do
      [ "${NODE_OF[$c]:-?}" = "$F_NODE" ] || continue
      [[ "$EXC" == *" $c "* ]] && continue
      sibs=$(expand "${SIB[$c]}" | sort -n)
      [ "$c" = "$(head -1 <<<"$sibs")" ] || continue
      echo "${USAGE[$c]:-0} $c"
    done | sort -k1,1n -k2,2nr | head -n "$SHARDS")
    pick=()
    while read -r u c; do
      [ -n "$c" ] || continue
      pick+=("$c")
      forced_usage+="$c:$u,"
    done <<<"$rank"
    forced=$(printf '%s\n' "${pick[@]}" | compress)
  fi
  forced_usage=${forced_usage%,}
fi

echo "FORCED=${forced:-NONE}"
echo "FORCED_NODE=$F_NODE"
echo "FORCED_USAGE=$forced_usage"
[ -n "$suggest" ] || exit 2
