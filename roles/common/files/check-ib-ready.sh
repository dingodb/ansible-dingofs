#!/usr/bin/env bash
# Ansible-managed: wait until InfiniBand / RDMA networking is healthy.
#
# Used as systemd ExecStartPre by dingo-cache and dingo-client. After a reboot
# (or an HCA reset) the RDMA stack and the cluster IP can come up well after
# network.target, so starting the service immediately leaves it in a crash loop.
#
# Ready means BOTH:
#   1. at least one InfiniBand/RDMA port reports ACTIVE in sysfs;
#   2. an IPv4 address matching IB_IP_PREFIX is assigned to an interface whose
#      administrative state is UP.
#
# Device names are deliberately NOT configured: HCA and IPoIB names differ per
# machine (ib0, ib6s200p0, mlx5_0, ...), so every RDMA device is considered.
#
# Environment:
#   IB_IP_PREFIX     IPv4 prefix that marks the cluster network (default 192.168.)
#   IB_WAIT_TIMEOUT  seconds to wait for both conditions (default 60, 0 = one check)
#   IB_POLL_INTERVAL poll interval in seconds (default 2)
#
# Exit: 0 = ready, 1 = timed out (systemd fails the start and retries).

set -u

IB_IP_PREFIX="${IB_IP_PREFIX:-192.168.}"
IB_WAIT_TIMEOUT="${IB_WAIT_TIMEOUT:-60}"
IB_POLL_INTERVAL="${IB_POLL_INTERVAL:-2}"

normalize_int() {
    case "$1" in
        ''|*[!0-9]*) echo "$2" ;;
        *) echo "$1" ;;
    esac
}

IB_WAIT_TIMEOUT="$(normalize_int "$IB_WAIT_TIMEOUT" 60)"
IB_POLL_INTERVAL="$(normalize_int "$IB_POLL_INTERVAL" 2)"
[ "$IB_POLL_INTERVAL" -ge 1 ] || IB_POLL_INTERVAL=1

has_active_rdma_port() {
    local state_file
    for state_file in /sys/class/infiniband/*/ports/*/state; do
        [ -r "$state_file" ] || continue
        grep -q 'ACTIVE' "$state_file" 2>/dev/null && return 0
    done
    return 1
}

# Print the interface name of every IPv4 address whose value starts with
# IB_IP_PREFIX. `ip -o` keeps each address on one line: "2: eth0 inet ...".
matching_ip_interfaces() {
    ip -4 -o addr show 2>/dev/null | awk -v prefix="$IB_IP_PREFIX" '
        $3 == "inet" && index($4, prefix) == 1 {
            iface = $2
            sub(/:$/, "", iface)
            sub(/@.*/, "", iface)
            print iface
        }'
}

is_iface_up() {
    local iface="$1" flags
    flags=$(ip -o link show dev "$iface" 2>/dev/null |
            sed -n 's/.*<\([^>]*\)>.*/\1/p')
    case ",$flags," in
        *,UP,*) return 0 ;;
        *) return 1 ;;
    esac
}

ib_ready() {
    has_active_rdma_port || return 1

    local iface
    while read -r iface; do
        [ -n "$iface" ] || continue
        is_iface_up "$iface" && return 0
    done < <(matching_ip_interfaces)
    return 1
}

diagnose() {
    echo "InfiniBand not ready after ${IB_WAIT_TIMEOUT}s (IB_IP_PREFIX=${IB_IP_PREFIX})" >&2

    if [ -d /sys/class/infiniband ] && [ -n "$(ls -A /sys/class/infiniband 2>/dev/null)" ]; then
        local state_file
        for state_file in /sys/class/infiniband/*/ports/*/state; do
            [ -r "$state_file" ] || continue
            echo "  ${state_file} = $(cat "$state_file" 2>/dev/null)" >&2
        done
    else
        echo "  no RDMA devices in /sys/class/infiniband (driver not loaded?)" >&2
    fi

    echo "  IPv4 addresses matching ${IB_IP_PREFIX}:" >&2
    local found=0 iface addr flags
    while read -r iface; do
        [ -n "$iface" ] || continue
        found=1
        addr=$(ip -4 -o addr show dev "$iface" 2>/dev/null |
               awk '$3 == "inet" { print $4 }')
        flags=$(ip -o link show dev "$iface" 2>/dev/null |
                sed -n 's/.*<\([^>]*\)>.*/\1/p')
        echo "    ${iface}: ${addr:-no inet} flags=${flags:-unknown}" >&2
    done < <(matching_ip_interfaces)
    [ "$found" -eq 1 ] || echo "    (none)" >&2
}

start=$(date +%s)
deadline=$((start + IB_WAIT_TIMEOUT))

while :; do
    if ib_ready; then
        echo "IB ready: ACTIVE RDMA port and ${IB_IP_PREFIX} IPv4 interface is UP" \
             "(waited $(( $(date +%s) - start ))s)"
        exit 0
    fi

    now=$(date +%s)
    [ "$now" -ge "$deadline" ] && break

    remaining=$((deadline - now))
    sleep_for=$IB_POLL_INTERVAL
    [ "$sleep_for" -gt "$remaining" ] && sleep_for=$remaining
    sleep "$sleep_for"
done

diagnose
exit 1
