#!/usr/bin/env bash
#
# remap-dingo-mounts-by-size.sh
#
# Discover /dev/dingo_cache_<N>/lv entries in /etc/fstab, sort them by
# filesystem size ascending, and map them to /mnt/disk1 ... /mnt/diskN.
#
# Default: dry run
# Apply:   sudo bash remap-dingo-mounts-by-size.sh --apply
#
# Examples:
#   894G -> /mnt/disk1
#   3.5T -> /mnt/disk2
#   7.0T -> /mnt/disk3
#   7.0T -> /mnt/disk4
#
# Same-size LVs are ordered by their device name as a stable tie-breaker.
#

set -Eeuo pipefail
IFS=$'\n\t'

FSTAB="/etc/fstab"
APPLY=0
SKIP_CONFIRM=0
BACKUP=""
TMP_FSTAB=""

# Matches both legacy and device-mapper fstab notations:
#   /dev/dingo_cache_1/lv          (written by the lvm_cache role)
#   /dev/mapper/dingo_cache_1-lv   (hand-crafted / older hosts)
DINGO_FSTAB_DEV='^/dev/(dingo_cache_[0-9]+/lv|mapper/dingo_cache_[0-9]+-lv)$'

declare -a DEVICES=()
declare -a OLD_MOUNTS=()
declare -a NEW_MOUNTS=()

die() {
    echo "ERROR: $*" >&2
    exit 1
}

cleanup() {
    if [[ -n "$TMP_FSTAB" && -f "$TMP_FSTAB" ]]; then
        rm -f "$TMP_FSTAB"
    fi
    # Never let the EXIT trap override the script's real exit code:
    # the [[ ]] above returning 1 would otherwise turn exit 0 into RC=1.
    return 0
}
trap cleanup EXIT

usage() {
    cat <<'EOF'
Usage:
  remap-dingo-mounts-by-size.sh [--apply]

Options:
  --apply     Apply the remap:
              - verify disks
              - check active users
              - sync
              - unmount affected filesystems
              - update /etc/fstab
              - reload systemd
              - mount filesystems again
  --yes       Skip the interactive YES confirmation (for automation/ansible)

Without --apply, this is a safe dry run only. Exit code is 2 when a remap is
needed (dry run), 0 when the layout is already correct.

Examples:
  bash remap-dingo-mounts-by-size.sh
  sudo bash remap-dingo-mounts-by-size.sh --apply
EOF
}

is_expected_device_mounted() {
    local device="$1"
    local target="$2"
    local current_source

    current_source="$(findmnt -rn -o SOURCE --target "$target" 2>/dev/null || true)"

    [[ -n "$current_source" ]] || return 1

    # Handles both:
    #   /dev/dingo_cache_1/lv
    #   /dev/mapper/dingo_cache_1-lv
    [[ "$(readlink -f "$current_source")" == "$(readlink -f "$device")" ]]
}

fstab_has_expected_mapping() {
    local device="$1"
    local target="$2"

    awk -v dev="$device" -v mnt="$target" '
        /^[[:space:]]*#/ || NF < 2 { next }
        $1 == dev && $2 == mnt {
            found = 1
        }
        END {
            exit !found
        }
    ' "$FSTAB"
}

restore_original_layout() {
    echo "Restoring original /etc/fstab from: $BACKUP"

    [[ -n "$BACKUP" && -f "$BACKUP" ]] || {
        echo "WARNING: fstab backup is unavailable; manual recovery is required." >&2
        return 1
    }

    cp -a "$BACKUP" "$FSTAB"

    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload || true
    fi

    echo "Attempting to restore original mounts..."

    for new_mount in "${NEW_MOUNTS[@]}"; do
        if mountpoint -q "$new_mount"; then
            umount "$new_mount" || true
        fi
    done

    for old_mount in "${OLD_MOUNTS[@]}"; do
        mount "$old_mount" || true
    done
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply)
            APPLY=1
            ;;
        --yes)
            SKIP_CONFIRM=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "Unknown argument: $1"
            ;;
    esac
    shift
done

[[ -f "$FSTAB" ]] || die "Cannot find $FSTAB"

if [[ "$APPLY" -eq 1 && "$EUID" -ne 0 ]]; then
    die "Run with sudo when using --apply"
fi

for cmd in awk sort findmnt blockdev mountpoint mount umount readlink; do
    command -v "$cmd" >/dev/null 2>&1 || die "Required command not found: $cmd"
done

# Find only Dingo cache entries such as:
# /dev/dingo_cache_1/lv /mnt/disk1 xfs defaults 0 0
mapfile -t FSTAB_ENTRIES < <(
    awk -v DINGO_FSTAB_DEV="$DINGO_FSTAB_DEV" '
        /^[[:space:]]*#/ || NF < 2 { next }

        $1 ~ DINGO_FSTAB_DEV &&
        $2 ~ /^\/mnt\/disk[0-9]+$/ {
            print $1 "|" $2
        }
    ' "$FSTAB"
)

[[ "${#FSTAB_ENTRIES[@]}" -gt 0 ]] || die \
    "No matching /dev/dingo_cache_N/lv and /mnt/diskN entries found in $FSTAB"

declare -a SORT_INPUT=()

for entry in "${FSTAB_ENTRIES[@]}"; do
    device="${entry%%|*}"
    old_mount="${entry##*|}"

    [[ -b "$device" ]] || die "Block device does not exist: $device"
    [[ -d "$old_mount" ]] || die "Mountpoint does not exist: $old_mount"

    # Sort using exact byte counts instead of human-readable df output.
    size_bytes="$(blockdev --getsize64 "$device")"

    # Fields:
    # size_bytes | device | existing mountpoint
    SORT_INPUT+=("${size_bytes}|${device}|${old_mount}")
done

# Ascending numeric size; device path is a deterministic tie-breaker.
mapfile -t SORTED < <(
    printf '%s\n' "${SORT_INPUT[@]}" |
        sort -t'|' -k1,1n -k2,2
)

echo "Detected Dingo cache LVs, ordered by size:"
printf '%-6s %-30s %-12s %s\n' \
    "Rank" "Device" "Size" "New mountpoint"

rank=1

for line in "${SORTED[@]}"; do
    size_bytes="${line%%|*}"
    remainder="${line#*|}"

    device="${remainder%%|*}"
    old_mount="${remainder##*|}"
    new_mount="/mnt/disk${rank}"

    [[ -d "$new_mount" ]] || die \
        "Required target mountpoint does not exist: $new_mount"

    if command -v numfmt >/dev/null 2>&1; then
        human_size="$(numfmt --to=iec-i --suffix=B "$size_bytes")"
    else
        human_size="${size_bytes}B"
    fi

    DEVICES+=("$device")
    OLD_MOUNTS+=("$old_mount")
    NEW_MOUNTS+=("$new_mount")

    printf '%-6s %-30s %-12s %s\n' \
        "$rank" "$device" "$human_size" "$new_mount"

    ((rank++))
done

echo
echo "Current mount layout:"

for i in "${!DEVICES[@]}"; do
    device="${DEVICES[$i]}"
    new_mount="${NEW_MOUNTS[$i]}"

    current_source="$(
        findmnt -rn -o SOURCE --target "$new_mount" 2>/dev/null || true
    )"

    printf '%-30s currently on %-35s -> planned %s\n' \
        "$device" "${current_source:-NOT_MOUNTED}" "$new_mount"
done

echo
echo "Proposed fstab mapping:"

for i in "${!DEVICES[@]}"; do
    printf '%-30s %s xfs defaults 0 0\n' \
        "${DEVICES[$i]}" "${NEW_MOUNTS[$i]}"
done

echo
echo "Checking whether current layout is already correct..."

layout_correct=1
fstab_correct=1

for i in "${!DEVICES[@]}"; do
    device="${DEVICES[$i]}"
    new_mount="${NEW_MOUNTS[$i]}"

    if is_expected_device_mounted "$device" "$new_mount"; then
        echo "OK: runtime mount: $device -> $new_mount"
    else
        echo "NEEDS CHANGE: runtime mount: $device -> $new_mount"
        layout_correct=0
    fi

    if fstab_has_expected_mapping "$device" "$new_mount"; then
        echo "OK: fstab mapping:  $device -> $new_mount"
    else
        echo "NEEDS CHANGE: fstab mapping: $device -> $new_mount"
        fstab_correct=0
    fi
done

echo

# Idempotent exit:
# No unmount, no fstab rewrite, no remount if everything already matches.
if [[ "$layout_correct" -eq 1 && "$fstab_correct" -eq 1 ]]; then
    echo "Disk layout is already correct."
    echo "No unmount, fstab update, or remount operation is required."
    exit 0
fi

if [[ "$APPLY" -eq 0 ]]; then
    echo "Dry run only; no filesystem or /etc/fstab changes were made."
    echo
    echo "After confirming the mapping and stopping applications using these disks:"
    echo "  sudo bash $0 --apply"
    exit 2
fi

echo "The layout requires changes."
if [[ "$SKIP_CONFIRM" -eq 0 ]]; then
    read -r -p "Type YES to unmount affected filesystems and apply the mapping: " answer
    [[ "$answer" == "YES" ]] || die "Cancelled."
else
    echo "--yes given, applying (fstab backup will still be created before changes)."
fi

echo
echo "Validating current mounts before changes..."

# Verify each currently configured source is mounted on the mountpoint
# recorded in the original fstab. This protects against unexpected layouts.
for i in "${!DEVICES[@]}"; do
    device="${DEVICES[$i]}"
    old_mount="${OLD_MOUNTS[$i]}"

    current_source="$(
        findmnt -rn -o SOURCE --target "$old_mount" 2>/dev/null || true
    )"

    [[ -n "$current_source" ]] || die \
        "Expected old mountpoint is not mounted: $old_mount"

    [[ "$(readlink -f "$current_source")" == "$(readlink -f "$device")" ]] || die \
        "Unexpected source on $old_mount: expected $device, got $current_source"
done

echo "Checking for processes using affected filesystems..."

# Check each unique old mountpoint once.
declare -A CHECKED_MOUNTS=()

for old_mount in "${OLD_MOUNTS[@]}"; do
    [[ -n "${CHECKED_MOUNTS[$old_mount]:-}" ]] && continue
    CHECKED_MOUNTS["$old_mount"]=1

    if command -v fuser >/dev/null 2>&1; then
        if fuser -m "$old_mount" >/dev/null 2>&1; then
            echo
            echo "Processes are using $old_mount:"
            fuser -vm "$old_mount" || true
            die "Stop these processes before running the remap."
        fi
    fi
done

# Flush ONLY the dingo-cache mounts: a global sync(1) iterates EVERY
# filesystem superblock, so a stalled unrelated mount (ceph/k8s volumes) can
# hang it in an unkillable D state and block the whole remap (seen on a node
# whose kernel-ceph mount stopped answering). sync(<mountpoint>) targets just
# that filesystem; blockdev --flushbufs is a best-effort device-level flush.
echo "Flushing pending filesystem writes (dingo-cache mounts only)..."
for i in "${!DEVICES[@]}"; do
    sync "${OLD_MOUNTS[$i]}" 2>/dev/null || true
    blockdev --flushbufs "${DEVICES[$i]}" 2>/dev/null || true
done

BACKUP="/etc/fstab.dingo-remap.$(date +%Y%m%d-%H%M%S)"
TMP_FSTAB="$(mktemp /etc/fstab.dingo-remap.XXXXXX)"

echo "Creating fstab backup: $BACKUP"
cp -a "$FSTAB" "$BACKUP"

# Generate AWK assignments:
# target["/dev/dingo_cache_1/lv"]="/mnt/disk3";
awk_map=""

for i in "${!DEVICES[@]}"; do
    awk_map+="target[\"${DEVICES[$i]}\"]=\"${NEW_MOUNTS[$i]}\";"
done

awk "
BEGIN {
    $awk_map
}
{
    # Change only the mountpoint field of discovered Dingo cache entries.
    if (\$1 in target) {
        \$2 = target[\$1]
    }
    print
}
" "$FSTAB" > "$TMP_FSTAB"

echo "Validating generated fstab..."

# Warnings about currently unreachable network mounts are acceptable.
# Actual parser errors cause a non-zero exit status.
findmnt --verify --tab-file "$TMP_FSTAB" || die \
    "Generated fstab validation failed; original fstab was not changed."

echo "Unmounting affected filesystems..."

# Unmount unique old mountpoints once. The layout can only be changed
# safely after all old mountpoints are unmounted.
declare -A UNMOUNTED_MOUNTS=()

for old_mount in "${OLD_MOUNTS[@]}"; do
    [[ -n "${UNMOUNTED_MOUNTS[$old_mount]:-}" ]] && continue
    UNMOUNTED_MOUNTS["$old_mount"]=1

    umount "$old_mount" || {
        echo "Unmount failed for $old_mount."
        echo "Original fstab is unchanged: $FSTAB"
        exit 1
    }
done

echo "Installing updated /etc/fstab..."
cat "$TMP_FSTAB" > "$FSTAB"

if command -v systemctl >/dev/null 2>&1; then
    echo "Reloading systemd configuration..."
    systemctl daemon-reload || {
        echo "systemctl daemon-reload failed."
        restore_original_layout
        exit 1
    }
fi

echo "Mounting remapped filesystems..."

# Mount only these target paths, avoiding unrelated network or remote
# fstab entries that may be temporarily unavailable.
for new_mount in "${NEW_MOUNTS[@]}"; do
    if ! mount "$new_mount"; then
        echo "Mount failed for $new_mount."
        restore_original_layout
        exit 1
    fi
done

echo
echo "Verifying final layout..."

for i in "${!DEVICES[@]}"; do
    device="${DEVICES[$i]}"
    new_mount="${NEW_MOUNTS[$i]}"

    if ! is_expected_device_mounted "$device" "$new_mount"; then
        echo "Final validation failed: $device is not mounted on $new_mount."
        restore_original_layout
        exit 1
    fi
done

echo
echo "Final mount layout:"
# findmnt accepts a single target per invocation; loop over mountpoints so a
# multi-argument call cannot fail under 'set -e' after a successful remap.
for mountpoint in "${NEW_MOUNTS[@]}"; do
    findmnt -rn -o SOURCE,SIZE,USED,AVAIL,TARGET "$mountpoint" || true
done

echo
echo "Final Dingo entries in /etc/fstab:"
awk -v DINGO_FSTAB_DEV="$DINGO_FSTAB_DEV" '
    /^[[:space:]]*#/ || NF < 2 { next }

    $1 ~ DINGO_FSTAB_DEV &&
    $2 ~ /^\/mnt\/disk[0-9]+$/ {
        print
    }
' "$FSTAB"

echo
echo "Success."
echo "Original fstab backup: $BACKUP"
