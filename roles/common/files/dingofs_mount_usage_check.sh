#!/usr/bin/env bash
#
# dingofs_mount_usage_check.sh
# Managed by Ansible - do not edit manually
#
# Check whether a DingoFS mountpoint is in use before it is stopped or
# remounted. Three independent checks are performed:
#
#   1. host processes with files open on the mountpoint (fuser -m);
#   2. running containers in Kubernetes namespaces beginning with 'cci',
#      including their actual mount namespace and retained bind mounts;
#   3. with --strict, ALL containers and other process mount namespaces.
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
  --strict        Inspect ALL containers and other process mount namespaces.
                  Default: only Kubernetes namespaces beginning with 'cci'.
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
        log "ERROR: fuser is required to establish mount usage."
        return 2
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
    python3 - "$mp" <<'PYDEV'
import os, sys
value = os.stat(sys.argv[1]).st_dev
print("%s:%s" % (os.major(value), os.minor(value)))
PYDEV
}

namespace_users_busy() {
    local mp=$1 target_dev host_ns mountinfo pid ns line
    local mid parent dev root mnt rest comm cgroup found=0

    target_dev=$(stat_dev "$mp") || {
        log "ERROR: cannot stat $mp."
        return 2
    }
    [ -d /proc ] || return 2

    host_ns=$(readlink /proc/1/ns/mnt 2>/dev/null || true)

    for mountinfo in /proc/[0-9]*/mountinfo; do
        if [ ! -r "$mountinfo" ]; then
            [ -e "$mountinfo" ] && return 2
            continue
        fi
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
        if [ -d /var/lib/kubelet ]; then
            log "ERROR: Kubernetes host requires crictl for mount checks."
            return 2
        fi
        return 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        log "ERROR: python3 is required for container checks."
        return 2
    fi

    python3 - "$mp" "$strict" <<'PY'
import json
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path


def log(message):
    from datetime import datetime
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message))


def cri(*args):
    return json.loads(subprocess.check_output(
        ["crictl"] + list(args), stderr=subprocess.DEVNULL, timeout=60))


def at_or_below(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def mountinfo(path):
    rows = []
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        separator = fields.index("-")
        if separator < 6 or len(fields) < separator + 4:
            raise ValueError("invalid mountinfo at " + str(path))
        unescape = lambda value: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)
        rows.append({"device": fields[2], "path": unescape(fields[4]),
                     "fstype": fields[separator + 1]})
    return rows


def mounted_device(target):
    # An unmounted directory shares the root filesystem with unrelated paths.
    # Only use filesystem identity if this exact target is a real mountpoint.
    if any(row["path"] == target for row in mountinfo("/proc/self/mountinfo")):
        device = os.stat(target).st_dev
        return "%s:%s" % (os.major(device), os.minor(device))
    return None


def container_references(detail, target, target_device):
    info = detail.get("info") or {}
    mounts = (detail.get("status") or {}).get("mounts") or []
    if not mounts:
        mounts = (info.get("runtimeSpec") or {}).get("mounts") or []
    hits, ancestors = [], []
    for mount in mounts:
        source = mount.get("hostPath") or mount.get("source") or ""
        destination = mount.get("containerPath") or mount.get("destination") or ""
        # OCI pseudo-filesystem sources such as 'proc' are not host paths.
        if not isinstance(source, str) or not os.path.isabs(source):
            continue
        source = os.path.realpath(source)
        label = "hostPath=%s -> %s" % (source, destination)
        if at_or_below(source, target):
            # Keep detecting direct references after a host mount is detached.
            hits.append(label + " (target path)")
        elif at_or_below(target, source):
            if not isinstance(destination, str) or not posixpath.isabs(destination):
                raise ValueError("ancestor hostPath has no absolute container destination")
            mapped = posixpath.normpath(posixpath.join(destination, os.path.relpath(target, source)))
            ancestors.append((mapped, label))
        elif target_device is not None:
            try:
                device = os.stat(source).st_dev
            except FileNotFoundError:
                continue
            if "%s:%s" % (os.major(device), os.minor(device)) == target_device:
                hits.append(label + " (same mounted filesystem)")
    if hits:
        return hits
    if not ancestors and target_device is None:
        return []
    # A container binding '/' does not automatically hold every possible future
    # child mount. Inspect its namespace for an actual mapped mount, including
    # a retained FUSE mount that has already been detached on the host.
    pid = str(info.get("pid", ""))
    if not re.fullmatch(r"[1-9][0-9]*", pid):
        raise ValueError("container PID is unavailable; cannot inspect mount namespace")
    rows = mountinfo("/proc/%s/mountinfo" % pid)
    for mapped, label in ancestors:
        for row in rows:
            if at_or_below(row["path"], mapped):
                hits.append(label + " (mounted in container at %s)" % row["path"])
    if target_device is not None and not hits:
        # Covers bind aliases that no longer exist at their original host path.
        for row in rows:
            if row["device"] == target_device:
                hits.append("mount_path_in_ns=%s (same mounted filesystem)" % row["path"])
    return hits


def check(target, strict=False):
    target = os.path.realpath(target)
    target_device = mounted_device(target)
    pod_response = cri("pods", "-o", "json")
    container_response = cri("ps", "-o", "json", "--state", "Running")
    for response, key in [(pod_response, "items"), (container_response, "containers")]:
        if not isinstance(response, dict) or (response and key not in response):
            raise ValueError("unrecognized CRI response for " + key)
    pods = pod_response.get("items", [])
    containers = container_response.get("containers", [])
    if not isinstance(pods, list) or not isinstance(containers, list):
        raise ValueError("invalid CRI list response")
    pod_meta = {}
    for pod in pods:
        meta = pod.get("metadata") or {}
        pod_meta[pod.get("id", "")] = (meta.get("namespace", ""), meta.get("name", ""))
    hits = []
    for container in containers:
        namespace, pod_name = pod_meta.get(container.get("podSandboxId", ""), ("", ""))
        if not strict:
            if not namespace:
                raise ValueError("cannot determine Kubernetes namespace for container " + container.get("id", ""))
            if not namespace.startswith("cci"):
                continue
        container_id = container.get("id", "")
        if not container_id:
            raise ValueError("running container has no ID")
        detail = cri("inspect", "-o", "json", container_id)
        for reference in container_references(detail, target, target_device):
            hits.append("    pod=%s/%s container=%s %s" %
                        (namespace, pod_name, container_id[:13], reference))
    if hits:
        log("IN USE (containers referencing %s, via crictl):" % target)
        for hit in hits:
            log(hit)
        return 1
    scope = "all namespaces" if strict else "'cci*' namespaces"
    log("OK: no container in %s references %s." % (scope, target))
    return 0


def main():
    try:
        return check(sys.argv[1], sys.argv[2] == "1")
    except Exception as exc:
        print("ERROR: cannot establish container mount usage: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
PY
    # Python contract: 0 = idle, 1 = busy. This script's callers use the
    # opposite convention (0 = busy), so invert it here.
    local rc=$?
    case "$rc" in
        0) return 1 ;;
        1) return 0 ;;
        *) return 2 ;;
    esac
}

# --- Main --------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    log "ERROR: this script must be run as root (fuser/systemd need privileges)."
    exit 2
fi

log "Checking $MOUNTPOINT"
if $STRICT; then
    log "Container scope: all Kubernetes namespaces (--strict)."
else
    log "Container scope: Kubernetes namespaces beginning with cci."
fi

if [ ! -e "$MOUNTPOINT" ] || ! mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
    # Containers may retain a bind mount after the host path was detached.
    if container_users_busy "$MOUNTPOINT"; then
        log "BUSY: a container still references $MOUNTPOINT."
        exit 1
    else
        check_rc=$?
        if [ "$check_rc" -ne 1 ]; then
            log "UNKNOWN: cannot establish container usage for $MOUNTPOINT."
            exit 2
        fi
    fi
    log "IDLE: host path is unmounted and no container references it."
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
unknown=false

if host_processes_busy "$MOUNTPOINT"; then
    busy=true
else
    [ "$?" -eq 1 ] || unknown=true
    log "Host process check completed."
fi

# The unrestricted /proc scan would count infrastructure containers even when
# the caller selected cci-only policy. Default mode inspects namespaces only
# for the selected CRI containers in container_users_busy instead.
if $STRICT; then
    if namespace_users_busy "$MOUNTPOINT"; then
        busy=true
    else
        [ "$?" -eq 1 ] || unknown=true
    fi
fi

if container_users_busy "$MOUNTPOINT"; then
    busy=true
else
    [ "$?" -eq 1 ] || unknown=true
fi

if $busy; then
    log "BUSY: skipping restart of ${SERVICE:-the associated service}. Drain/delete the users above first."
    exit 1
fi

if $unknown; then
    log "UNKNOWN: mount usage could not be established."
    exit 2
fi
log "IDLE: nothing is using $MOUNTPOINT."
if $DRY_RUN; then
    log "DRY-RUN: would restart ${SERVICE:-the associated service} now."
fi
exit 0
