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
#      standalone containerd namespaces are inspected without a cci filter;
#   3. with --strict, ALL containers and other process mount namespaces.
#
# Usage:
#   dingofs_mount_usage_check.sh <mount_point> [--dry-run] [--strict]
#                                  [--ignore-stale-mounts] [--service NAME]
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
IGNORE_STALE=false
MOUNTPOINT=""
SERVICE_OVERRIDE=""

usage() {
    cat <<EOF
Usage: $PROG <mount_point> [--dry-run] [--strict] [--ignore-stale-mounts] [--service NAME]

  <mount_point>          Absolute path of the DingoFS mount to inspect.
  --dry-run              Report the decision without changing anything (default behaviour).
  --strict               Inspect ALL containers and other process mount namespaces.
                         Default: only Kubernetes namespaces beginning with 'cci'.
  --ignore-stale-mounts  Ignore private DingoFS references with a confirmed
                         ENOTCONN error. Other references still block restart.
  --service NAME         Use NAME instead of auto-detecting the systemd unit.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --strict) STRICT=true ;;
        --ignore-stale-mounts) IGNORE_STALE=true ;;
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
    local mp=$1 units unit load state sub description mount_path owner=''
    command -v systemctl >/dev/null 2>&1 || return 1
    units=$(systemctl list-units 'dingofs-mount-*.service' --all --plain --full \
                --no-legend --no-pager) || return 2
    while read -r unit load state sub description; do
        case "$state" in ''|inactive|failed) continue ;; esac
        mount_path=$(mount_script_mount "$unit") || continue
        [ "$mount_path" = "$mp" ] || continue
        if [ "$state/$sub" != active/running ] || [ -n "$owner" ]; then
            log "UNKNOWN: cannot select one running service for $mp ($unit is $state/$sub, previous match: ${owner:-none})." >&2
            return 2
        fi
        owner=$unit
    done <<< "$units"
    [ -n "$owner" ] || return 1
    printf '%s\n' "$owner"
}

# --- Check 1: host processes with open files --------------------------------
host_processes_busy() {
    local mp=$1 output rc=0
    if ! command -v fuser >/dev/null 2>&1; then
        log "ERROR: fuser is required to establish mount usage."
        return 2
    fi
    output=$(fuser -m "$mp" 2>&1) || rc=$?
    if [ "$rc" -eq 0 ]; then
        fuser -vm "$mp" 2>&1 | while IFS= read -r line; do
            [ -n "$line" ] && log "    $line"
        done
        return 0
    fi
    if [ "$rc" -eq 1 ] && [ -z "$output" ]; then
        return 1
    fi
    log "ERROR: fuser could not establish usage for $mp (rc=$rc): $output"
    return 2
}

# --- Check 2: other mount namespaces holding the same filesystem ------------
# Bind mounts (container hostPath) keep the source superblock's st_dev, so
# comparing major:minor catches paths that differ from the host mountpoint.
stat_dev() {
    local mp=$1
    python3 - "$mp" <<'PYDEV'
import os, posixpath, re, sys
from pathlib import Path

target = posixpath.normpath(sys.argv[1])
mi = Path("/proc/self/mountinfo")
if mi.exists():
    unescape = lambda value: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)
    rows = []
    for line in mi.read_text().splitlines():
        fields = line.split()
        if len(fields) >= 5:
            rows.append((unescape(fields[4]), fields[2]))
    matches = [dev for path, dev in rows if path == target]
    if matches:
        print(matches[-1])
        sys.exit(0)
    parent, name = posixpath.split(target)
    if name:
        candidate = posixpath.normpath(posixpath.join(os.path.realpath(parent), name))
        if candidate != target:
            matches = [dev for path, dev in rows if path == candidate]
            if matches:
                print(matches[-1])
                sys.exit(0)
    sys.exit(1)
value = os.stat(target).st_dev
print("%s:%s" % (os.major(value), os.minor(value)))
PYDEV
}

is_host_mounted() {
    local mp=$1
    if [ -r /proc/self/mountinfo ] && command -v python3 >/dev/null 2>&1; then
        stat_dev "$mp" >/dev/null 2>&1
        return $?
    fi
    [ -e "$mp" ] && mountpoint -q "$mp" 2>/dev/null
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
# Runtime inspection is done in python3: it is far less error-prone than
# shelling out once per field, and python3 is already required by Ansible.
container_users_busy() {
    local mp=$1 strict=0 ignore_stale=0
    ${STRICT:-false} && strict=1
    ${IGNORE_STALE:-false} && ignore_stale=1

    if ! command -v python3 >/dev/null 2>&1; then
        log "ERROR: python3 is required for container checks."
        return 2
    fi

    python3 - "$mp" "$strict" "$ignore_stale" <<'PY'
import errno
import json
import os
import posixpath
import re
import shutil
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
        rows.append({
            "id": fields[0],
            "device": fields[2],
            "root": unescape(fields[3]),
            "path": unescape(fields[4]),
            "optional": fields[6:separator],
            "fstype": fields[separator + 1],
            "source": unescape(fields[separator + 2]),
        })
    return rows


def host_mountinfo():
    path = Path("/proc/self/mountinfo")
    if not path.exists():
        return []
    return mountinfo(path)


def peer_groups(row):
    groups = set()
    for field in row.get("optional") or ():
        if field.startswith("shared:") or field.startswith("master:"):
            groups.add(field.split(":", 1)[1])
    return groups


def canonical_host_path(path, host_rows=None):
    norm = posixpath.normpath(path)
    if host_rows is None:
        host_rows = host_mountinfo()
    # Never stat/lstat at or below a mounted FUSE path: a stalled FUSE daemon
    # would hang os.path.realpath(). Resolve symlinked parents outside mounts.
    mounted_paths = {row["path"] for row in host_rows}
    if norm in mounted_paths:
        return norm
    fuse_mounts = sorted(
        (row["path"] for row in host_rows if row.get("fstype", "").startswith("fuse")),
        key=len,
        reverse=True,
    )
    for mp in fuse_mounts:
        if at_or_below(norm, mp):
            return norm
    parent, name = posixpath.split(norm)
    if name:
        resolved_parent = os.path.realpath(parent)
        candidate = posixpath.normpath(posixpath.join(resolved_parent, name))
        if candidate in mounted_paths:
            return candidate
        for mp in fuse_mounts:
            if at_or_below(candidate, mp):
                return candidate
    return os.path.realpath(norm)


def mounted_device(target):
    # Read major:minor directly from /proc/self/mountinfo instead of calling
    # os.stat(target), which can hang if the target FUSE daemon is stalled.
    # An unmounted directory has no mountinfo entry and returns None.
    matches = [row for row in mountinfo("/proc/self/mountinfo") if row["path"] == target]
    return matches[-1]["device"] if matches else None


def target_peer_groups_for(host_rows, target, target_device):
    if target_device is None:
        return set()
    matches = [
        row for row in host_rows
        if row["path"] == target and row["device"] == target_device
    ]
    return peer_groups(matches[-1]) if matches else set()


def host_device_for_path(path, host_rows):
    best = None
    for row in host_rows:
        if at_or_below(path, row["path"]):
            if best is None or len(row["path"]) >= len(best["path"]):
                best = row
    if best is not None:
        return best["device"]
    try:
        device = os.stat(path).st_dev
    except FileNotFoundError:
        return None
    return "%s:%s" % (os.major(device), os.minor(device))


def unique_dingofs_mount(host_rows, target, target_device):
    matches = [row for row in host_rows if row["path"] == target]
    if len(matches) == 1:
        row = matches[0]
        if (row["device"] == target_device
                and row["fstype"] in ("fuse", "fuse.dingofs")
                and row["source"].startswith("DingoFS:")):
            return row
    return None


def confirms_disconnect(pid, row, rows):
    # Use the container's view of the mount and bound the FUSE metadata probe.
    # A different device ID alone says nothing about whether a daemon is alive.
    probe = """
import errno, os, sys
try:
    os.stat(sys.argv[1])
except OSError as error:
    sys.exit(errno.ENOTCONN if error.errno == errno.ENOTCONN else 1)
"""
    path = row["path"]
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe, "/proc/%s/root%s" % (pid, path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != errno.ENOTCONN:
        return False
    # A mount change during the probe invalidates the reference scan.
    return mountinfo("/proc/%s/mountinfo" % pid) == rows


def mount_usage(row, rows, pid, target_device, target_peers, host_mount):
    if target_device is None:
        return "BUSY", "host-unmounted"
    if row["device"] == target_device:
        return "LIVE", "dev=%s" % row["device"]
    if target_peers and (peer_groups(row) & target_peers):
        return "LIVE", "dev=%s shared-propagation" % row["device"]
    difference = "dev=%s != host %s" % (row["device"], target_device)
    if (host_mount is not None
            and row["fstype"] in ("fuse", "fuse.dingofs")
            and row.get("source") == host_mount["source"]
            and not row.get("optional")
            and sum(mount["path"] == row["path"] for mount in rows) == 1
            and confirms_disconnect(pid, row, rows)):
        return "STALE", difference + ", ENOTCONN"
    return "BUSY", difference + ", disconnect unconfirmed"


def classify_container_references(detail, target, target_device,
                                  target_peers=None, host_rows=None):
    target_peers = set(target_peers or ())
    host_rows = [] if host_rows is None else host_rows
    info = detail.get("info") or {}
    mounts = (detail.get("status") or {}).get("mounts") or []
    if not mounts:
        mounts = (info.get("runtimeSpec") or {}).get("mounts") or []
    direct, ancestors = [], []
    for mount in mounts:
        source = mount.get("hostPath") or mount.get("source") or ""
        destination = mount.get("containerPath") or mount.get("destination") or ""
        # OCI pseudo-filesystem sources such as 'proc' are not host paths.
        if not isinstance(source, str) or not os.path.isabs(source):
            continue
        source = canonical_host_path(source, host_rows)
        label = "hostPath=%s -> %s" % (source, destination)
        if at_or_below(source, target):
            # Keep detecting direct references after a host mount is detached.
            direct.append((destination, label, "target path"))
        elif at_or_below(target, source):
            if not isinstance(destination, str) or not posixpath.isabs(destination):
                raise ValueError("ancestor hostPath has no absolute container destination")
            mapped = posixpath.normpath(posixpath.join(destination, os.path.relpath(target, source)))
            ancestors.append((mapped, label))
        elif target_device is not None:
            if host_device_for_path(source, host_rows) == target_device:
                direct.append((destination, label, "same mounted filesystem"))
    if target_device is None and not ancestors:
        return [
            ("BUSY", "[BUSY host-unmounted] %s (%s)" % (label, kind))
            for _, label, kind in direct
        ]
    # Inspect the container's actual mount namespace whenever the host target
    # is mounted (to distinguish LIVE vs STALE superblocks and catch bind
    # aliases) or an ancestor bind mount may carry a retained child mount.
    pid = str(info.get("pid", ""))
    if not re.fullmatch(r"[1-9][0-9]*", pid):
        raise ValueError("container PID is unavailable; cannot inspect mount namespace")
    rows = mountinfo("/proc/%s/mountinfo" % pid)
    host_mount = unique_dingofs_mount(host_rows, target, target_device)
    results = []
    covered_live_paths = set()

    def add_reference(row, label):
        state, reason = mount_usage(row, rows, pid, target_device, target_peers, host_mount)
        results.append((state, "[%s %s] %s" % (state, reason, label)))
        if state == "LIVE":
            covered_live_paths.add(row["path"])

    for destination, label, kind in direct:
        if target_device is None:
            results.append(("BUSY", "[BUSY host-unmounted] %s (%s)" % (label, kind)))
            continue
        if not isinstance(destination, str) or not posixpath.isabs(destination):
            results.append(("BUSY", "[BUSY mountinfo-unmatched] %s (%s)" % (label, kind)))
            continue
        dest_norm = posixpath.normpath(destination)
        if not any(row["path"] == dest_norm for row in rows):
            results.append(("BUSY", "[BUSY mountinfo-unmatched] %s (%s)" % (label, kind)))
            continue
        for row in rows:
            if at_or_below(row["path"], dest_norm):
                add_reference(row, "%s (%s, mounted at %s)" % (label, kind, row["path"]))
    for mapped, label in ancestors:
        for row in rows:
            if at_or_below(row["path"], mapped):
                add_reference(row, "%s (mounted in container at %s)" % (label, row["path"]))
    if target_device is not None:
        # Always check the entire container mount namespace for any remaining
        # LIVE mounts of target_device (covers bind aliases that no longer
        # exist at their original host path, or LIVE aliases coexisting with
        # a STALE direct mount in the same container).
        for row in rows:
            if row["device"] == target_device and row["path"] not in covered_live_paths:
                covered_live_paths.add(row["path"])
                results.append((
                    "LIVE",
                    "[LIVE dev=%s] mount_path_in_ns=%s (same mounted filesystem)" % (row["device"], row["path"]),
                ))
    return results


def container_references(detail, target, target_device,
                         target_peers=None, host_rows=None, ignore_stale=False):
    refs = classify_container_references(
        detail, target, target_device,
        target_peers=target_peers, host_rows=host_rows,
    )
    return [text for state, text in refs if not (ignore_stale and state == "STALE")]


def check_cri(target, strict=False, ignore_stale=False):
    host_rows = host_mountinfo()
    target = canonical_host_path(target, host_rows)
    target_device = mounted_device(target)
    target_peers = target_peer_groups_for(host_rows, target, target_device)
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
    live_hits, stale_hits = [], []
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
        for state, reference in classify_container_references(
                detail, target, target_device,
                target_peers=target_peers, host_rows=host_rows):
            entry = "    pod=%s/%s container=%s %s" % (
                namespace, pod_name, container_id[:13], reference)
            if state == "STALE":
                stale_hits.append(entry)
            else:
                live_hits.append(entry)
    if live_hits:
        log("IN USE (containers referencing %s, via crictl):" % target)
        for hit in live_hits + stale_hits:
            log(hit)
        return 1
    scope = "all namespaces" if strict else "'cci*' namespaces"
    if stale_hits:
        if ignore_stale:
            log("STALE ONLY (ignoring stale container mounts referencing %s, via crictl; 0 LIVE):" % target)
            for hit in stale_hits:
                log(hit)
            log("OK: no LIVE container in %s references %s." % (scope, target))
            return 0
        log("IN USE (containers referencing %s, via crictl; STALE only, pass --ignore-stale-mounts to allow restart):" % target)
        for hit in stale_hits:
            log(hit)
        return 1
    log("OK: no container in %s references %s." % (scope, target))
    return 0


def ctr(*args):
    return subprocess.check_output(['ctr'] + list(args), stderr=subprocess.DEVNULL,
                                   timeout=60, text=True)


def check_ctr(target, strict=False, ignore_stale=False):
    host_rows = host_mountinfo()
    target = canonical_host_path(target, host_rows)
    target_device = mounted_device(target)
    target_peers = target_peer_groups_for(host_rows, target, target_device)
    live_hits, stale_hits = [], []
    for runtime_namespace in ctr('namespaces', 'list', '-q').split():
        lines = [line.split() for line in ctr('-n', runtime_namespace, 'tasks', 'list').splitlines() if line.strip()]
        if not lines or lines[0][:3] != ['TASK', 'PID', 'STATUS']:
            raise ValueError('unrecognized ctr task list for ' + runtime_namespace)
        for row in lines[1:]:
            if len(row) != 3:
                raise ValueError('invalid ctr task row')
            container_id, pid, state = row
            if state in ('STOPPED', 'EXITED'):
                continue
            if state not in ('RUNNING', 'PAUSED', 'PAUSING', 'CREATED'):
                raise ValueError('unrecognized ctr task state: ' + state)
            if not re.fullmatch(r'[1-9][0-9]*', pid):
                raise ValueError('active ctr task has no valid PID')
            detail = json.loads(ctr('-n', runtime_namespace, 'containers', 'info', container_id))
            labels = detail.get('Labels', detail.get('labels', {})) or {}
            spec = detail.get('Spec', detail.get('spec'))
            if not isinstance(spec, dict):
                raise ValueError('ctr container OCI spec is unavailable')
            annotations = spec.get('annotations') or {}
            namespace = labels.get('io.kubernetes.pod.namespace') or annotations.get('io.kubernetes.cri.sandbox-namespace') or ''
            pod = labels.get('io.kubernetes.pod.name') or annotations.get('io.kubernetes.cri.sandbox-name') or container_id
            kubernetes = runtime_namespace == 'k8s.io' or bool(namespace) or labels.get('io.cri-containerd.kind') in ('sandbox', 'container')
            if kubernetes and not strict:
                if not namespace:
                    raise ValueError('cannot determine Kubernetes namespace for ctr task ' + container_id)
                if not namespace.startswith('cci'):
                    continue
            mounts = spec.get('mounts', [])
            if not isinstance(mounts, list):
                raise ValueError('invalid ctr OCI mounts')
            adapted = {'info': {'pid': int(pid), 'runtimeSpec': {'mounts': mounts}}}
            for ref_state, reference in classify_container_references(
                    adapted, target, target_device,
                    target_peers=target_peers, host_rows=host_rows):
                entry = '    runtime_ns=%s pod=%s/%s container=%s %s' % (
                    runtime_namespace, namespace or 'non-kubernetes', pod, container_id[:13], reference)
                if ref_state == 'STALE':
                    stale_hits.append(entry)
                else:
                    live_hits.append(entry)
    if live_hits:
        log('IN USE (containers referencing %s, via ctr):' % target)
        for hit in live_hits + stale_hits:
            log(hit)
        return 1
    if stale_hits:
        if ignore_stale:
            log('STALE ONLY (ignoring stale container mounts referencing %s, via ctr; 0 LIVE):' % target)
            for hit in stale_hits:
                log(hit)
            log('OK: ctr found no LIVE selected running container referencing ' + target)
            return 0
        log('IN USE (containers referencing %s, via ctr; STALE only, pass --ignore-stale-mounts to allow restart):' % target)
        for hit in stale_hits:
            log(hit)
        return 1
    log('OK: ctr found no selected running container referencing ' + target)
    return 0


def runtime_present():
    sockets = ['/run/containerd/containerd.sock', '/run/k3s/containerd/containerd.sock',
               '/var/run/crio/crio.sock', '/run/crio/crio.sock',
               '/var/run/cri-dockerd.sock', '/var/run/docker.sock', '/run/podman/podman.sock']
    if any(Path(path).exists() for path in sockets):
        return True
    for comm in Path('/proc').glob('[0-9]*/comm'):
        try:
            if comm.read_text().strip() in ('kubelet', 'containerd', 'dockerd', 'crio', 'cri-dockerd', 'conmon'):
                return True
        except FileNotFoundError:
            continue
    # A stale /var/lib/kubelet directory alone is not a running runtime.
    return False


def check_plain_namespaces(target, ignore_stale=False):
    host_rows = host_mountinfo()
    target = canonical_host_path(target, host_rows)
    target_device = mounted_device(target)
    target_peers = target_peer_groups_for(host_rows, target, target_device)
    host_mount = unique_dingofs_mount(host_rows, target, target_device)
    host_namespace = os.readlink('/proc/1/ns/mnt')
    seen = {host_namespace, os.readlink('/proc/self/ns/mnt')}
    ambiguous = False
    live_hits, stale_hits = [], []
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            namespace = os.readlink(process / 'ns/mnt')
            if namespace in seen:
                continue
            rows = mountinfo(process / 'mountinfo')
        except FileNotFoundError:
            if process.exists():
                raise ValueError('live process mount namespace is unreadable')
            continue
        seen.add(namespace)
        for row in rows:
            if target_device is not None and row['device'] == target_device:
                live_hits.append('pid=%s path=%s [LIVE dev=%s]' % (process.name, row['path'], row['device']))
            elif at_or_below(row['path'], target):
                state, reason = mount_usage(row, rows, process.name, target_device, target_peers, host_mount)
                hit = 'pid=%s path=%s [%s %s]' % (process.name, row['path'], state, reason)
                if state == 'STALE':
                    stale_hits.append(hit)
                else:
                    live_hits.append(hit)
            elif target_device is None and row['fstype'].startswith('fuse') and row.get('source', '').startswith('DingoFS:'):
                ambiguous = True
    if live_hits:
        for hit in live_hits + stale_hits:
            log('IN USE (process namespace): ' + hit)
        return 1
    if stale_hits:
        if ignore_stale:
            for hit in stale_hits:
                log('STALE ONLY (ignoring stale process namespace mount): ' + hit)
            log('OK: no container runtime detected; no LIVE process mount namespace references %s.' % target)
            return 0
        for hit in stale_hits:
            log('IN USE (process namespace; STALE only, pass --ignore-stale-mounts to allow restart): ' + hit)
        return 1
    if ambiguous:
        raise ValueError('foreign namespace retains a DingoFS mount; cannot correlate an unmounted host path')
    log('OK: no container runtime detected; process mount namespaces checked.')
    return 0


def check(target, strict=False, ignore_stale=False):
    errors = []
    cri_ok = False
    if shutil.which('crictl'):
        try:
            if (check_cri(target, strict, ignore_stale) if ignore_stale else check_cri(target, strict)) == 1:
                return 1
            cri_ok = True
        except Exception as error:
            errors.append('crictl: ' + str(error))
            log('CRI query unavailable; trying containerd/ctr if present.')
    if shutil.which('ctr'):
        try:
            return check_ctr(target, strict, ignore_stale) if ignore_stale else check_ctr(target, strict)
        except Exception as error:
            errors.append('ctr: ' + str(error))
    if cri_ok and not errors:
        return 0
    if errors or runtime_present():
        raise ValueError('; '.join(errors) or 'a container runtime/kubelet is active but neither crictl nor ctr can inspect it')
    return check_plain_namespaces(target, ignore_stale) if ignore_stale else check_plain_namespaces(target)


def main():
    try:
        ignore_stale = len(sys.argv) > 3 and sys.argv[3] == "1"
        return check(sys.argv[1], sys.argv[2] == "1", ignore_stale)
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
    log "Container scope: all containers and process mount namespaces (--strict)."
else
    log "Container scope: Kubernetes cci* namespaces and standalone containerd tasks."
fi
if $IGNORE_STALE; then
    log "Only confirmed disconnected mounts may be ignored (--ignore-stale-mounts)."
fi

if ! is_host_mounted "$MOUNTPOINT"; then
    # Containers may retain a bind mount after the host path was detached.
    if IGNORE_STALE=false container_users_busy "$MOUNTPOINT"; then
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
    SERVICE=$(detect_service "$MOUNTPOINT") || {
        if [ "$?" -ne 1 ]; then
            log "UNKNOWN: cannot establish the running systemd service for $MOUNTPOINT."
            exit 2
        fi
    }
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
