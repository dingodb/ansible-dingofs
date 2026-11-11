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


def all_ports():
    return sorted({int(p.name) for p in Path('/sys/class/infiniband').glob('*/ports/*')
                   if p.name.isdigit()})


def netdev_devices(netdev):
    """RDMA devices exposed by a netdev under /sys/class/net/<netdev>.

    RoCE netdevs link back to their PCI device here. IPoIB netdevs do not,
    and are found by the reverse device-to-netdev mapping in resolve().
    """
    base = Path('/sys/class/net') / netdev / 'device/infiniband'
    if not base.is_dir():
        return []
    return sorted(entry.name for entry in base.iterdir())


def safe_resolve(name, port):
    try:
        return resolve(name, port)
    except ValueError:
        return None


def device_for_prefix(prefix):
    """Return the one RDMA device/port that carries IB_IP_PREFIX.

    The interface address decides the fabric, matching the readiness check.
    When the prefix covers more than one ready device the answer is ambiguous,
    so this never guesses a device name.
    """
    prefix = prefix.strip()
    if not prefix:
        raise ValueError('IB_IP_PREFIX is empty; set the InfiniBand IPv4 prefix')
    links = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show'], timeout=10))
    ports = all_ports()
    matches = {}
    for link in links:
        if not any(address.get('family') == 'inet' and address.get('local', '').startswith(prefix)
                   for address in link.get('addr_info', [])):
            continue
        netdev = link['ifname'].split('@')[0]
        names = netdev_devices(netdev)
        if not names:
            names = sorted({mapping['device'] for mapping in
                            (safe_resolve(netdev, port) for port in ports) if mapping})
        for name in names:
            device_ports = [port for port in ports
                            if (Path('/sys/class/infiniband') / name / 'ports' / str(port)).exists()]
            ready_ports = [mapping for mapping in
                           (safe_resolve(name, port) for port in device_ports)
                           if mapping and netdev in mapping['netdevs']]
            if not ready_ports and len(device_ports) == 1:
                # A single-port HCA is unambiguous even without gid ndevs data.
                fallback = safe_resolve(name, device_ports[0])
                ready_ports = [fallback] if fallback else []
            if len(ready_ports) != 1:
                raise ValueError('cannot pick one port on RDMA device %s for %s '
                                 '(IB_IP_PREFIX %s)' % (name, netdev, prefix))
            matches[(name, ready_ports[0]['port'])] = ready_ports[0]
    if not matches:
        raise ValueError('no RDMA device carries an interface in IB_IP_PREFIX %s' % prefix)
    if len(matches) != 1:
        raise ValueError('IB_IP_PREFIX %s maps to %s RDMA device/port pairs; narrow the '
                         'prefix or set the RDMA device explicitly' % (prefix, len(matches)))
    return next(iter(matches.values()))


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
    if len(sys.argv) > 2 and sys.argv[1] == '--device-for-prefix':
        print(json.dumps(device_for_prefix(sys.argv[2])))
        return 0
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
            ports = all_ports()
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
