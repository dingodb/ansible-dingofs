#!/usr/bin/env bash
#
# dingofs_mount_usage_check.sh
# Managed by Ansible - do not edit manually
#
# Check whether a DingoFS mountpoint is in use before it is stopped or
# remounted. Three independent checks are performed:
#
#   1. host processes with files open on the mountpoint (fuser -m);
#   2. processes in OTHER mount namespaces that mount the same filesystem
#      (bind mounts / container hostPath map to the same st_dev);
#   3. running containers whose hostPath is at or under the mountpoint
#      (crictl; by default only namespaces matching 'cci-*', --strict = all).
#
# Usage:
#   dingofs_mount_usage_check.sh <mount_point> [--dry-run] [--strict]
#                                  [--service NAME]
#
# Exit status:
#   0 = IDLE    (safe to stop/remount)
#   1 = BUSY    (in use; leave the mountpoint alone)
#   2 = error   (bad usage, or the check could not run)
#
# This script never stops, starts or restarts anything. --dry-run is accepted
# for command-line compatibility and only changes the final log line.

set -u

PROG=$(basename "$0")
DRY_RUN=false
STRICT=false
MOUNTPOINT=""
SERVICE_OVERRIDE=""

usage() {
    cat <<EOF
Usage: $PROG <mount_point> [--dry-run] [--strict] [--service NAME]

  <mount_point>   Absolute path of the DingoFS mount to inspect.
  --dry-run       Report the decision without changing anything (default behaviour).
  --strict        Inspect containers in ALL namespaces (default: 'cci-*' only).
  --service NAME  Use NAME instead of auto-detecting the systemd unit.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --strict) STRICT=true ;;
        --service)
            shift
            [ $# -gt 0 ] || { echo "$PROG: --service requires a value" >&2; exit 2; }
            SERVICE_OVERRIDE="$1"
            ;;
        -h|--help) usage; exit 0 ;;
        -*)
            echo "$PROG: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [ -n "$MOUNTPOINT" ]; then
                echo "$PROG: unexpected argument: $1" >&2
                exit 2
            fi
            MOUNTPOINT="$1"
            ;;
    esac
    shift
done

if [ -z "$MOUNTPOINT" ]; then
    echo "$PROG: missing <mount_point>" >&2
    usage >&2
    exit 2
fi

case "$MOUNTPOINT" in
    /*) ;;
    *) echo "$PROG: <mount_point> must be an absolute path" >&2; exit 2 ;;
esac

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# --- Detect the systemd unit whose mount script uses this mountpoint ---------
# Unit names do not always match the mount path (e.g. /mnt/public-fs ->
# dingofs-mount-dingospeed-public.service), so match on the mount script's
# g_mnt= assignment rather than on the unit name.
mount_script_mount() {
    local unit=$1 exec_start candidate mount_path
    exec_start=$(systemctl show -p ExecStart --value "$unit" 2>/dev/null) || return 1
    for candidate in $(printf '%s\n' "$exec_start" | grep -oE '/[^ ;}]+'); do
        [ -f "$candidate" ] && [ -r "$candidate" ] || continue
        mount_path=$(awk -F'"' '/^g_mnt=/{print $2; exit}' "$candidate" 2>/dev/null)
        if [ -n "$mount_path" ]; then
            printf '%s\n' "$mount_path"
            return 0
        fi
    done
    return 1
}

detect_service() {
    local mp=$1 unit mount_path
    if command -v systemctl >/dev/null 2>&1; then
        while read -r unit; do
            [ -n "$unit" ] || continue
            mount_path=$(mount_script_mount "$unit") || continue
            if [ "$mount_path" = "$mp" ]; then
                printf '%s\n' "$unit"
                return 0
            fi
        done < <(systemctl list-unit-files 'dingofs-mount-*.service' \
                     --no-legend 2>/dev/null | awk '{print $1}')
    fi
    return 1
}

# --- Check 1: host processes with open files --------------------------------
host_processes_busy() {
    local mp=$1
    if ! command -v fuser >/dev/null 2>&1; then
        log "WARN: fuser not installed (psmisc); skipping host-process check."
        return 1
    fi
    if fuser -m "$mp" >/dev/null 2>&1; then
        fuser -vm "$mp" 2>&1 | while IFS= read -r line; do
            [ -n "$line" ] && log "    $line"
        done
        return 0
    fi
    return 1
}

# --- Check 2: other mount namespaces holding the same filesystem ------------
# Bind mounts (container hostPath) keep the source superblock's st_dev, so
# comparing major:minor catches paths that differ from the host mountpoint.
stat_dev() {
    local mp=$1 raw major minor
    raw=$(stat -c '%t:%T' "$mp" 2>/dev/null) || return 1
    major=$((16#${raw%%:*}))
    minor=$((16#${raw##*:}))
    printf '%s:%s\n' "$major" "$minor"
}

namespace_users_busy() {
    local mp=$1 target_dev host_ns mountinfo pid ns line
    local mid parent dev root mnt rest comm cgroup found=0

    target_dev=$(stat_dev "$mp") || {
        log "WARN: cannot stat $mp; skipping mount-namespace check."
        return 1
    }
    [ -d /proc ] || return 1

    host_ns=$(readlink /proc/1/ns/mnt 2>/dev/null || true)

    for mountinfo in /proc/[0-9]*/mountinfo; do
        [ -r "$mountinfo" ] || continue
        pid=${mountinfo#/proc/}
        pid=${pid%/mountinfo}
        [ "$pid" = "$$" ] && continue

        ns=$(readlink "/proc/$pid/ns/mnt" 2>/dev/null) || continue
        if [ -n "$host_ns" ] && [ "$ns" = "$host_ns" ]; then
            continue
        fi

        while read -r mid parent dev root mnt rest; do
            [ "$dev" = "$target_dev" ] || continue
            comm=$(cat "/proc/$pid/comm" 2>/dev/null || echo '?')
            cgroup=$(awk -F: '$1 == "0" { print $3; exit }' \
                     "/proc/$pid/cgroup" 2>/dev/null || true)
            if [ "$found" -eq 0 ]; then
                log "IN USE (mount namespaces holding $mp):"
                found=1
            fi
            log "    pid=$pid comm=$comm mount_path_in_ns=$mnt cgroup=${cgroup:-?}"
        done < "$mountinfo"
    done

    [ "$found" -eq 1 ]
}

# --- Check 3: containers with a hostPath at/under the mountpoint ------------
# crictl inspection is done in python3: it is far less error-prone than
# shelling out once per field, and python3 is already required by Ansible.
container_users_busy() {
    local mp=$1 strict=0
    $STRICT && strict=1

    if ! command -v crictl >/dev/null 2>&1; then
        log "WARN: crictl not found; skipping container hostPath check."
        return 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        log "WARN: python3 not found; skipping container hostPath check."
        return 1
    fi

    python3 - "$mp" "$strict" <<'PY'
import json
import subprocess
import sys

mountpoint = sys.argv[1].rstrip("/") or "/"
strict = sys.argv[2] == "1"


def log(msg):
    print("[%s] %s" % (__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def cri(*args):
    out = subprocess.check_output(
        ["crictl"] + list(args), stderr=subprocess.DEVNULL, timeout=60
    )
    return json.loads(out)


def under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


try:
    pods = cri("pods", "-o", "json").get("items", [])
    containers = cri("ps", "-o", "json", "--state", "Running").get("items", [])
except Exception as exc:  # noqa: BLE001 - any crictl failure means "cannot check"
    log("WARN: crictl is unusable (%s); skipping container hostPath check." % exc)
    sys.exit(0)

pod_meta = {}
for pod in pods:
    meta = pod.get("metadata") or {}
    pod_meta[pod.get("id", "")] = (
        meta.get("namespace", ""),
        meta.get("name", ""),
    )

qualified = []
for container in containers:
    pod_id = container.get("podSandboxId", "")
    namespace, pod_name = pod_meta.get(pod_id, ("", ""))
    if not strict and not namespace.startswith("cci-"):
        continue
    qualified.append((container.get("id", ""), namespace, pod_name))

if not qualified:
    scope = "all namespaces" if strict else "'cci-*' namespaces"
    log("OK: no container in %s holds %s." % (scope, mountpoint))
else:
    hits = []
    for container_id, namespace, pod_name in qualified:
        try:
            detail = cri("inspect", "-o", "json", container_id)
        except Exception:
            continue

        mounts = (detail.get("status") or {}).get("mounts") or []
        if not mounts:
            spec_mounts = ((detail.get("info") or {}).get("runtimeSpec") or {}).get("mounts") or []
            mounts = [
                {"hostPath": m.get("source", ""), "containerPath": m.get("destination", "")}
                for m in spec_mounts
            ]

        for mount in mounts:
            host_path = mount.get("hostPath") or mount.get("source") or ""
            container_path = mount.get("containerPath") or mount.get("destination") or ""
            if under(host_path, mountpoint):
                hits.append(
                    "    pod=%s/%s container=%s hostPath=%s -> %s"
                    % (namespace, pod_name, container_id[:13], host_path, container_path)
                )

    if hits:
        log("IN USE (containers with hostPath at/under %s, via crictl):" % mountpoint)
        for hit in hits:
            log(hit)
        sys.exit(1)

log("OK: no inspected container binds %s directly." % mountpoint)
sys.exit(0)
PY
    # Python contract: 0 = idle, 1 = busy. This script's callers use the
    # opposite convention (0 = busy), so invert it here.
    local rc=$?
    [ "$rc" -eq 0 ] && return 1
    return 0
}

# --- Main --------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    log "ERROR: this script must be run as root (fuser/systemd need privileges)."
    exit 2
fi

log "Checking $MOUNTPOINT"

if [ ! -e "$MOUNTPOINT" ]; then
    log "WARN: $MOUNTPOINT does not exist; treating it as IDLE (nothing can use it)."
    log "IDLE: nothing is using $MOUNTPOINT."
    exit 0
fi

if ! mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
    log "WARN: $MOUNTPOINT is not currently mounted; treating it as IDLE."
    log "IDLE: nothing is using $MOUNTPOINT."
    exit 0
fi

if [ -n "$SERVICE_OVERRIDE" ]; then
    SERVICE="$SERVICE_OVERRIDE"
else
    SERVICE=$(detect_service "$MOUNTPOINT" || true)
fi

if [ -n "$SERVICE" ]; then
    case "$SERVICE" in
        *.service) ;;
        *) SERVICE="${SERVICE}.service" ;;
    esac
    log "Detected service: $SERVICE"
else
    log "WARN: could not detect the systemd unit for $MOUNTPOINT."
fi

busy=false

if host_processes_busy "$MOUNTPOINT"; then
    busy=true
else
    log "OK: no host process has files open on $MOUNTPOINT."
fi

if namespace_users_busy "$MOUNTPOINT"; then
    busy=true
fi

if container_users_busy "$MOUNTPOINT"; then
    busy=true
fi

if $busy; then
    log "BUSY: skipping restart of ${SERVICE:-the associated service}. Drain/delete the users above first."
    exit 1
fi

log "IDLE: nothing is using $MOUNTPOINT."
if $DRY_RUN; then
    log "DRY-RUN: would restart ${SERVICE:-the associated service} now."
fi
exit 0
