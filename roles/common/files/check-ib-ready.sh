#!/usr/bin/env bash
exec python3 - "$@" <<'PYRDMA'
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def resolve(name, port):
    base = Path('/sys/class/infiniband')
    matches = []
    for device in base.iterdir():
        endpoint = device / 'ports' / str(port)
        if not endpoint.exists():
            continue
        netdevs = set()
        for attribute in (endpoint / 'gid_attrs/ndevs').glob('*'):
            try:
                name_value = attribute.read_text().strip()
            except OSError as error:
                # Empty/reserved GID slots are exposed as EINVAL by Linux.
                if error.errno in (errno.EINVAL, errno.ENOENT, errno.ENODEV):
                    continue
                raise
            if name_value:
                netdevs.add(name_value)
        if not netdevs:
            for netdev in (device / 'device/net').glob('*'):
                for field in ('dev_port', 'dev_id'):
                    attribute = netdev / field
                    if attribute.exists():
                        if int(attribute.read_text().strip(), 0) == port - 1:
                            netdevs.add(netdev.name)
                        break
                else:
                    if len(list((device / 'ports').iterdir())) == 1:
                        netdevs.add(netdev.name)
        if name == device.name or name in netdevs:
            matches.append((device, endpoint, sorted(netdevs)))
    if len(matches) != 1:
        raise ValueError('RDMA device/port mapping is missing or ambiguous for %s/%s' % (name, port))
    device, endpoint, netdevs = matches[0]
    numa = None
    numa_source = 'unknown'
    location = (device / 'device').resolve()
    for parent in (location, *location.parents):
        attribute = parent / 'numa_node'
        if attribute.exists():
            value = int(attribute.read_text())
            if value >= 0:
                numa = value
                numa_source = 'device' if parent == location else 'pci-parent'
                break
    if numa is None:
        # Some virtual/PCI-passthrough devices report -1. A single online
        # system node is unambiguous; never infer node 0 on multi-node hosts.
        online_path = Path('/sys/devices/system/node/online')
        if online_path.exists():
            online_nodes = set()
            for span in online_path.read_text().strip().split(','):
                ends = span.split('-')
                online_nodes.update(range(int(ends[0]), int(ends[-1]) + 1))
            if len(online_nodes) == 1:
                only = next(iter(online_nodes))
                cpus = Path('/sys/devices/system/node/node%s/cpulist' % only)
                if cpus.exists() and cpus.read_text().strip():
                    numa, numa_source = only, 'single-online-node'
    return {'device': device.name, 'port': port, 'netdevs': netdevs, 'numa_node': numa,
            'numa_source': numa_source,
            'state_path': str(endpoint / 'state')}


def ready(mapping, prefix, links=None):
    if Path(mapping['state_path']).read_text().strip().split(':')[0] != '4':
        return False
    if links is None:
        links = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show'], timeout=10))
    return any(link['ifname'].split('@')[0] in mapping['netdevs'] and 'UP' in link.get('flags', [])
               and any(address.get('family') == 'inet' and address.get('local', '').startswith(prefix)
                       for address in link.get('addr_info', [])) for link in links)


def device_status(name):
    """Kernel sysfs decides existence; ibv_devices only confirms the verbs stack.

    The optional utilities package may be absent on a working host, so a
    missing binary must not be reported as a missing device. A binary that
    exists but fails or hangs is a different problem and is reported as such,
    so a broken verbs stack is visible instead of looking like a clean host.
    """
    if not (Path('/sys/class/infiniband') / name).exists():
        return 'missing-kernel'
    try:
        output = subprocess.check_output(['ibv_devices'], stderr=subprocess.PIPE, timeout=10)
    except FileNotFoundError:
        return 'present-no-ibv-devices'
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b'').decode(errors='replace').strip()
        print('ibv_devices exited %s: %s' % (error.returncode, detail or 'no stderr'), file=sys.stderr)
        return 'present-ibv-devices-error'
    except subprocess.TimeoutExpired:
        print('ibv_devices timed out after 10s', file=sys.stderr)
        return 'present-ibv-devices-error'
    except OSError as error:
        print('ibv_devices could not be executed: %s' % error, file=sys.stderr)
        return 'present-ibv-devices-error'
    return 'present' if name in output.decode(errors='replace').split() else 'missing-verbs'


def main():
    if len(sys.argv) > 2 and sys.argv[1] == '--device-status':
        print(device_status(sys.argv[2]))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == '--resolve':
        print(json.dumps(resolve(sys.argv[2], int(sys.argv[3]))))
        return 0
    timeout_value = os.environ.get('IB_WAIT_TIMEOUT', '60')
    timeout = int(timeout_value) if timeout_value.isdigit() else 60
    interval_value = os.environ.get('IB_POLL_INTERVAL', '2')
    interval = max(1, int(interval_value)) if interval_value.isdigit() else 2
    deadline = time.monotonic() + timeout
    prefix = os.environ.get('IB_IP_PREFIX', '192.168.')
    while True:
        try:
            # The fabric is healthy when exactly one RDMA port backs an UP
            # interface in IB_IP_PREFIX. The configured device name is
            # deliberately ignored: device naming differs per host and a stale
            # name must not decide whether the network works.
            links = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show'], timeout=10))
            ports = {int(p.name) for p in Path('/sys/class/infiniband').glob('*/ports/*') if p.name.isdigit()}
            candidates = {}
            for link in links:
                if not any(a.get('local', '').startswith(prefix) for a in link.get('addr_info', [])):
                    continue
                for candidate_port in sorted(ports):
                    try:
                        candidate = resolve(link['ifname'].split('@')[0], candidate_port)
                    except ValueError:
                        continue
                    if ready(candidate, prefix, links):
                        candidates[(candidate['device'], candidate_port)] = candidate
            if len(candidates) != 1:
                raise ValueError('IB_IP_PREFIX %s must map to exactly one ready RDMA device; found %s'
                                 % (prefix, len(candidates)))
            mapping = next(iter(candidates.values()))
            print('RDMA ready ' + json.dumps(mapping))
            return 0
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            reason = str(error)
        if time.monotonic() >= deadline:
            print(reason, file=sys.stderr)
            return 1
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

PYRDMA
