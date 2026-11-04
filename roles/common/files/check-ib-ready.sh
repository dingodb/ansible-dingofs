#!/usr/bin/env bash
exec python3 - "$@" <<'PYRDMA'
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
        netdevs = {p.read_text().strip() for p in (endpoint / 'gid_attrs/ndevs').glob('*')}
        if not netdevs:
            netdevs = {p.name for p in (device / 'device/net').glob('*')}
        if name == device.name or name in netdevs:
            matches.append((device, endpoint, sorted(netdevs)))
    if len(matches) != 1:
        raise ValueError('RDMA device/port mapping is missing or ambiguous for %s/%s' % (name, port))
    device, endpoint, netdevs = matches[0]
    numa = None
    location = (device / 'device').resolve()
    for parent in (location, *location.parents):
        attribute = parent / 'numa_node'
        if attribute.exists():
            value = int(attribute.read_text())
            if value >= 0:
                numa = value
                break
    return {'device': device.name, 'port': port, 'netdevs': netdevs, 'numa_node': numa,
            'state_path': str(endpoint / 'state')}


def ready(mapping, prefix):
    if Path(mapping['state_path']).read_text().strip().split(':')[0] != '4':
        return False
    links = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show'], timeout=10))
    return any(link['ifname'].split('@')[0] in mapping['netdevs'] and 'UP' in link.get('flags', [])
               and any(address.get('family') == 'inet' and address.get('local', '').startswith(prefix)
                       for address in link.get('addr_info', [])) for link in links)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--resolve':
        print(json.dumps(resolve(sys.argv[2], int(sys.argv[3]))))
        return 0
    name = os.environ.get('RDMA_DEVICE', '')
    port = int(os.environ.get('RDMA_PORT_NUM', '1'))
    deadline = time.monotonic() + int(os.environ.get('IB_WAIT_TIMEOUT', '60'))
    prefix = os.environ.get('IB_IP_PREFIX', '192.168.')
    while True:
        try:
            if name:
                mapping = resolve(name, port)
            else:
                # Older units supplied only the business IP prefix.
                links = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show'], timeout=10))
                candidates = {}
                for link in links:
                    if not any(a.get('local', '').startswith(prefix) for a in link.get('addr_info', [])):
                        continue
                    ports = {port}
                    if 'RDMA_PORT_NUM' not in os.environ:
                        ports = {int(p.name) for p in Path('/sys/class/infiniband').glob('*/ports/*') if p.name.isdigit()}
                    for candidate_port in sorted(ports):
                        try:
                            candidate = resolve(link['ifname'].split('@')[0], candidate_port)
                        except ValueError:
                            continue
                        if ready(candidate, prefix):
                            candidates[(candidate['device'], candidate_port)] = candidate
                if len(candidates) != 1:
                    raise ValueError('business IP must map to exactly one RDMA device; configure RDMA_DEVICE')
                mapping = next(iter(candidates.values()))
            if ready(mapping, prefix):
                print('RDMA ready ' + json.dumps(mapping))
                return 0
            reason = 'configured RDMA port or associated IP interface is not ready'
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            reason = str(error)
        if time.monotonic() >= deadline:
            print(reason, file=sys.stderr)
            return 1
        time.sleep(min(2, max(0, deadline - time.monotonic())))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

PYRDMA
