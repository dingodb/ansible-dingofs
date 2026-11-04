#!/usr/bin/env bash
# One Python process collects CPU ownership and selects preferred/fallback NUMA.
exec python3 - "$@" <<'PYCPU'
"""Read host CPU ownership once, then select a NUMA-local set."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time


def expand(value):
    result = set()
    for part in value.strip().split(','):
        if not part:
            continue
        bounds = part.split('-')
        if len(bounds) > 2 or not all(x.isdigit() for x in bounds):
            raise ValueError('invalid CPU list: ' + value)
        first, last = int(bounds[0]), int(bounds[-1])
        if last < first:
            raise ValueError('reversed CPU range')
        result.update(range(first, last + 1))
    return result


def compact(values):
    values = sorted(values)
    spans = []
    for value in values:
        if spans and value == spans[-1][1] + 1:
            spans[-1][1] = value
        else:
            spans.append([value, value])
    return ','.join(str(a) if a == b else '%s-%s' % (a, b) for a, b in spans)


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def sample():
    result = {}
    for line in read('/proc/stat').splitlines():
        fields = line.split()
        if re.fullmatch(r'cpu\d+', fields[0]):
            values = list(map(int, fields[1:9]))
            result[int(fields[0][3:])] = (sum(values), values[3] + values[4])
    return result


def collect(args):
    online = expand(read('/sys/devices/system/cpu/online'))
    nodes = {}
    for path in Path('/sys/devices/system/node').glob('node[0-9]*/cpulist'):
        nodes[int(path.parent.name[4:])] = expand(read(path)) & online
    if not nodes:
        nodes = {0: online}
    siblings = {cpu: expand(read('/sys/devices/system/cpu/cpu%s/topology/thread_siblings_list' % cpu)) & online for cpu in online}
    claims = {}
    def claim(cpus, reason):
        for cpu in cpus:
            claims.setdefault(cpu, set()).add(reason)
    claim(expand(read('/sys/devices/system/cpu/isolated', '')), 'kernel-isolated')
    minimum = min(len(cpus) for cpus in nodes.values() if cpus)
    v2 = Path('/sys/fs/cgroup/cgroup.controllers').exists()
    base = Path('/sys/fs/cgroup') if v2 else Path('/sys/fs/cgroup/cpuset')
    effective = 'cpuset.cpus.effective' if v2 else 'cpuset.effective_cpus'
    for directory, _, files in os.walk(base):
        if effective not in files or Path(directory) == base:
            continue
        if v2 and not read(Path(directory) / 'cpuset.cpus', ''):
            continue
        cpus = expand(read(Path(directory) / effective))
        if len(cpus) < minimum:
            claim(cpus, 'cgroup ' + directory)
    state = Path('/var/lib/kubelet/cpu_manager_state')
    if state.exists():
        for containers in json.loads(read(state)).get('entries', {}).values():
            for cpus in containers.values():
                claim(expand(cpus), 'kubelet-exclusive')
    for path in ['/etc/kubernetes/kubelet-conf.yml', '/var/lib/kubelet/config.yaml']:
        match = re.search(r'^\s*reservedSystemCPUs:\s*[\'"]?([0-9,-]+)', read(path, ''), re.M)
        if match:
            claim(expand(match.group(1)), 'kubelet-reserved')
    self_matches = 0
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            command = (process / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            if not command:
                continue
            if args.self_exe:
                executable = re.sub(r' \(deleted\)$', '', os.readlink(process / 'exe'))
                if os.path.realpath(executable) == os.path.realpath(args.self_exe):
                    self_matches += 1
                    continue
            match = re.search(r'--cpuset[= ]([^ ]+)', command)
            if match:
                claim(expand(match.group(1)), 'process ' + process.name)
            else:
                status = (process / 'status').read_text()
                match = re.search(r'^Cpus_allowed_list:\s*(\S+)', status, re.M)
                if match:
                    affinity = expand(match.group(1))
                    if len(affinity) < minimum:
                        claim(affinity, 'affinity ' + process.name)
        except FileNotFoundError:
            continue  # process exited during collection
    if self_matches > 1:
        raise ValueError("multiple instances share the self executable; use distinct instance binaries")
    usage = {}
    if not args.no_busy:
        before = sample()
        time.sleep(1)
        after = sample()
        for cpu in online:
            if cpu not in before or cpu not in after:
                continue
            total = after[cpu][0] - before[cpu][0]
            idle = after[cpu][1] - before[cpu][1]
            usage[cpu] = 100 * (total - idle) / total if total > 0 else 0
            if usage[cpu] > args.busy:
                claim({cpu}, 'busy %.1f%%' % usage[cpu])
    claim(expand(args.exclude), 'assigned-this-run')
    return online, nodes, siblings, claims, usage


def choose(nodes, siblings, unavailable, shards, preferred):
    cores = {}
    for node, cpus in nodes.items():
        cores[node] = sorted(cpu for cpu in cpus if cpu == min(siblings[cpu]) and not (siblings[cpu] & unavailable))
    node = preferred
    if node not in cores or len(cores[node]) < shards:
        node = max(cores, key=lambda n: (len(cores[n]), -n))
    return node, cores[node][-shards:] if len(cores[node]) >= shards else [], cores[node]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', dest='shards', type=int, default=8)
    parser.add_argument('-n', dest='node', type=int)
    parser.add_argument('-c', dest='check', default='')
    parser.add_argument('-b', dest='busy', type=float, default=30)
    parser.add_argument('-x', dest='exclude', default='')
    parser.add_argument('-S', dest='legacy_self_name', default='')
    parser.add_argument('--self-exe', default='')
    parser.add_argument('--siblings', action='store_true')
    parser.add_argument('--no-busy', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if args.shards < 1:
        raise ValueError('shards must be positive')
    online, nodes, siblings, claims, usage = collect(args)
    if args.check:
        cpus = expand(args.check)
        if not cpus:
            raise ValueError('empty cpuset')
        for cpu in sorted(cpus):
            if cpu not in online:
                print('CONFLICT %s NOT-ONLINE' % cpu)
            elif cpu in claims:
                print('CONFLICT %s %s' % (cpu, '; '.join(sorted(claims[cpu]))))
        print('NODES=' + ' '.join(str(n) for n, members in sorted(nodes.items()) if members & cpus))
        bad = not cpus <= online or bool(cpus & claims.keys())
        if not bad:
            print('OK ' + args.check)
        return int(bad)
    node, selected, cores = choose(nodes, siblings, set(claims), args.shards, args.node)
    pick = set().union(*(siblings[c] for c in selected)) if args.siblings else set(selected)
    print('NODE=%s' % node)
    print('FREE=' + compact(nodes[node] - claims.keys()))
    print('CORES=' + compact(cores))
    print('SUGGEST=' + (compact(pick) or 'NONE'))
    forced, forced_node = set(), ''
    if not pick and args.force:
        excluded = expand(args.exclude)
        forced_node, candidates, all_cores = choose(nodes, siblings, excluded, args.shards, args.node)
        if candidates:
            selected = sorted(all_cores, key=lambda c: (max(usage.get(s, 0) for s in siblings[c]), -c))[:args.shards]
            forced = set().union(*(siblings[c] for c in selected)) if args.siblings else set(selected)
    print('FORCED=' + (compact(forced) or 'NONE'))
    print('FORCED_NODE=' + str(forced_node))
    print('FORCED_USAGE=' + ','.join('%s:%.1f' % (c, usage.get(c, 0)) for c in sorted(forced)))
    return 0 if pick else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('CPU detection failed: ' + str(error), file=sys.stderr)
        sys.exit(3)

PYCPU
