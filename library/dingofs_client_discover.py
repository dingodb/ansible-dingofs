#!/usr/bin/python
"""Discover client evidence without accessing the contents of a FUSE mount."""
import os
import posixpath
import re
import shlex
import stat
import subprocess
from pathlib import Path


DOCUMENTATION = r'''
module: dingofs_client_discover
short_description: Discover DingoFS mounts, system services and client processes
description:
  - Reads the current mount namespace, system systemd manager and procfs.
  - Never starts services, executes startup scripts or probes mounted contents.
options:
  command_timeout:
    description: Timeout in seconds for each systemctl query.
    type: int
    default: 15
supports_check_mode: true
'''

CLIENT_BINARY = re.compile(r'^(?:dingo-client|dingofs_client|dingo-fuse)(?:[.-].*)?$')
CLIENT_UNIT = re.compile(r'^(?:dingofs-mount|dingofs-client|dingo-client)(?:[.@-]|$)')
CLIENT_HINT = re.compile(r'dingofs-mount|dingo-client|dingofs_client|dingo-fuse|dingofs[ -]+(?:mount|client)', re.I)


def unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def parse_mountinfo(data):
    mounts = []
    if not data.strip():
        raise ValueError('Empty kernel mount table')
    for line in data.splitlines():
        fields = line.split()
        separator = fields.index('-')
        if separator < 6 or len(fields) < separator + 4:
            raise ValueError('Malformed kernel mount table')
        fstype, source = fields[separator + 1:separator + 3]
        mounts.append(dict(mount_id=int(fields[0]), device=fields[2],
                           root=unescape(fields[3]), mount_point=unescape(fields[4]),
                           fstype=fstype, source=unescape(source),
                           read_only='ro' in fields[5].split(',')))
    return mounts


def is_fuse(mount):
    return mount['fstype'] == 'fuse' or mount['fstype'].startswith('fuse.')


def is_dingofs(mount):
    return is_fuse(mount) and (mount['source'].lower().startswith('dingofs:')
                              or mount['fstype'].lower() == 'fuse.dingofs')


def client_identity(argv):
    """Extract identity only; never return a full command line or credentials."""
    result = dict(fsname='', mount_point='')
    for i, token in enumerate(argv):
        if token.startswith(('mds://', 'local://')):
            result['fsname'] = token.rstrip('/').rsplit('/', 1)[-1]
            if i + 1 < len(argv) and argv[i + 1].startswith('/'):
                result['mount_point'] = argv[i + 1]
        flag = re.match(r'^--?(fsname|mountpoint|mount_point|mountPoint)(?:=(.*))?$', token)
        if flag:
            value = flag[2] if flag[2] is not None else (argv[i + 1] if i + 1 < len(argv) else '')
            result['fsname' if flag[1] == 'fsname' else 'mount_point'] = value
        # Older FUSE clients pass filesystem identity using -o fsname=NAME.
        if i and argv[i - 1] == '-o':
            for option in token.split(','):
                if option.startswith('fsname='):
                    result['fsname'] = re.sub(r'^DingoFS:', '', option.split('=', 1)[1])
    if not result['mount_point'] and argv and CLIENT_BINARY.match(posixpath.basename(argv[0])):
        # Legacy clients use the mountpoint as the final positional argument.
        if len(argv) > 1 and argv[-1].startswith('/') and not argv[-2].startswith('-'):
            result['mount_point'] = argv[-1]
    if result['mount_point'].startswith('/'):
        result['mount_point'] = posixpath.normpath(result['mount_point'])
    else:
        result['mount_point'] = ''
    return result


def collect_processes(proc, warnings):
    own_namespace = os.readlink(str(proc / 'self/ns/mnt'))
    processes = []
    denied = 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            with (entry / 'cmdline').open('rb') as stream:
                argv = [part.decode('utf-8', 'replace') for part in stream.read(131072).split(b'\0') if part]
            if not argv or not CLIENT_BINARY.match(posixpath.basename(argv[0])):
                continue
            namespace = os.readlink(str(entry / 'ns/mnt'))
            cgroup = (entry / 'cgroup').read_text()
            units = re.findall(r'(?:^|/)([^/\n]+[.]service)(?:/|$)', cgroup, re.M)
            processes.append(dict(pid=int(entry.name), binary=argv[0], service=units[-1] if units else '',
                                  mount_namespace='current' if namespace == own_namespace else 'other',
                                  **client_identity(argv)))
        except FileNotFoundError:
            continue  # Process exited during the snapshot.
        except PermissionError:
            denied += 1
        except OSError:
            warnings.append('Could not inspect client process {}'.format(entry.name))
    if denied:
        warnings.append('Permission denied reading {} process(es); process discovery is incomplete'.format(denied))
    return sorted(processes, key=lambda item: item['pid'])


def run_systemctl(args, timeout):
    env = dict(os.environ, LC_ALL='C', SYSTEMD_PAGER='cat')
    completed = subprocess.run(['systemctl', '--no-pager'] + args, capture_output=True,
                               text=True, timeout=timeout, env=env)
    if completed.returncode:
        # Do not propagate arbitrary command output into reports.
        raise ValueError('systemctl {} failed (rc={})'.format(args[0], completed.returncode))
    return completed.stdout


def script_identity(path, mounts):
    """Read literal variables from our startup scripts; no shell evaluation."""
    if not path.startswith('/'):
        return {}
    path = posixpath.normpath(path)
    if any(is_fuse(m) and (path == m['mount_point'] or path.startswith(m['mount_point'].rstrip('/') + '/'))
           for m in mounts):
        return {}
    # Reject symlinks (including parents) and nonregular files before reading.
    candidate = Path(path)
    if any(parent.is_symlink() for parent in [candidate] + list(candidate.parents)):
        return {}
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return {}
        text = stream.read(131072)
    values = {}
    for key, value in re.findall(r'^\s*(?:export\s+)?(g_mnt|g_fsname)=(.*?)\s*$', text, re.M):
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            continue
        if len(parts) == 1 and not re.search(r'[$`]', parts[0]):
            values['mount_point' if key == 'g_mnt' else 'fsname'] = parts[0]
    if 'mount_point' in values:
        values['mount_point'] = (posixpath.normpath(values['mount_point'])
                                 if values['mount_point'].startswith('/') else '')
    return values


def parse_properties(data):
    for block in re.split(r'\n\s*\n', data.strip()):
        properties = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if properties.get('Id'):
            yield properties


def collect_services(processes, mounts, timeout, warnings, runner=run_systemctl):
    names = {p['service'] for p in processes if p['service'] and p['mount_namespace'] == 'current'}
    services = {}
    try:
        for command in (['list-unit-files', '--type=service', '--no-legend'],
                        ['list-units', '--type=service', '--all', '--plain', '--no-legend']):
            for line in runner(command, timeout).splitlines():
                fields = line.split()
                # Templates cannot be shown without an instance. Loaded concrete
                # instances are discovered separately by list-units.
                if fields and fields[0].endswith('.service') and '@.service' not in fields[0]:
                    names.add(fields[0])
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        warnings.append('System service enumeration incomplete: {}'.format(type(exc).__name__))
    names = sorted(names)
    properties = 'Id,LoadState,ActiveState,SubState,UnitFileState,MainPID,ExecStart,Description'
    for start in range(0, len(names), 100):
        try:
            output = runner(['show', '--property=' + properties, '--'] + names[start:start + 100], timeout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            warnings.append('System service inspection incomplete (batch {}): {}'.format(start // 100 + 1, type(exc).__name__))
            continue
        for props in parse_properties(output):
            name = props['Id']
            related = [p for p in processes if p['service'] == name and p['mount_namespace'] == 'current']
            if not (CLIENT_UNIT.match(name) or any(str(p['pid']) == props.get('MainPID') for p in related) or CLIENT_HINT.search(
                    props.get('ExecStart', '') + ' ' + props.get('Description', ''))):
                continue
            identity = dict(fsname='', mount_point='')
            executable = re.search(r'(?:^|[ {;])path=([^;]+?)\s*;', props.get('ExecStart', ''))
            if executable:
                try:
                    identity.update(script_identity(executable[1], mounts))
                except OSError:
                    warnings.append('Could not read startup script for ' + name)
            # systemctl renders direct ExecStart argv separately from its path.
            arguments = re.search(r'argv\[\]=(.*?)(?:\s+;|$)', props.get('ExecStart', ''))
            if arguments:
                try:
                    direct = client_identity(shlex.split(arguments[1]))
                    identity.update({key: value for key, value in direct.items() if value})
                except ValueError:
                    pass
            # A live client is stronger evidence than script variables.
            if len(related) == 1:
                identity.update({key: related[0][key] for key in identity if related[0][key]})
            services[name] = dict(service=name, load_state=props.get('LoadState', 'unknown'),
                                  active_state=props.get('ActiveState', 'unknown'),
                                  sub_state=props.get('SubState', 'unknown'),
                                  enabled=props.get('UnitFileState', 'unknown'),
                                  main_pid=int(props.get('MainPID', '0') or 0), **identity)
    return [services[name] for name in sorted(services)]


def correlate(mounts, services, processes):
    discovered, other_fuse = [], []
    for mount in mounts:
        if not is_fuse(mount):
            continue
        live = [p for p in processes if p['mount_namespace'] == 'current'
                and p['mount_point'] == mount['mount_point']]
        identified = is_dingofs(mount) or (mount['fstype'] == 'fuse' and bool(live))
        if not identified:
            other_fuse.append(mount)
            continue
        row = dict(mount, fsname=mount['source'].split(':', 1)[1]
                   if mount['source'].lower().startswith('dingofs:') else '',
                   identity_evidence='mount_table' if is_dingofs(mount) else 'client_process')
        if not row['fsname'] and len(live) == 1:
            row['fsname'] = live[0]['fsname']
        live = [p for p in live if not p['fsname'] or not row['fsname'] or p['fsname'] == row['fsname']]
        known_services = {s['service'] for s in services}
        row['services'] = sorted({p['service'] for p in live if p['service'] in known_services} | {
            s['service'] for s in services if s['mount_point'] == mount['mount_point']
            and (not s['fsname'] or not row['fsname'] or s['fsname'] == row['fsname'])})
        row['pids'] = [p['pid'] for p in live]
        row['state'] = 'mounted'
        discovered.append(row)
    for service in services:
        matching = [m for m in discovered if service['service'] in m['services']]
        service['mount_points'] = sorted({m['mount_point'] for m in matching})
        service['mount_state'] = 'mounted' if matching else (
            'not_mounted' if service['mount_point'] else 'unknown')
        if not matching and service['mount_point']:
            occupied = [m for m in mounts if m['mount_point'] == service['mount_point']]
            if occupied:
                service['mount_state'] = 'different_filesystem'
    return sorted(discovered, key=lambda m: (m['mount_point'], m['mount_id'])), other_fuse


def discover(timeout=15, proc=Path('/proc')):
    warnings = []
    mounts = parse_mountinfo((proc / 'self/mountinfo').read_text())
    processes = collect_processes(proc, warnings)
    services = collect_services(processes, mounts, timeout, warnings)
    discovered, other_fuse = correlate(mounts, services, processes)
    return dict(changed=False, schema_version=1, mounts=discovered, services=services,
                processes=processes, other_fuse_mounts=other_fuse, warnings=warnings,
                summary=dict(dingofs_mounts=len(discovered), client_services=len(services),
                             client_processes=len(processes), other_fuse_mounts=len(other_fuse),
                             complete=not warnings))


def main():
    from ansible.module_utils.basic import AnsibleModule
    module = AnsibleModule(argument_spec=dict(command_timeout=dict(type='int', default=15)),
                           supports_check_mode=True)
    if module.params['command_timeout'] < 1:
        module.fail_json(msg='command_timeout must be positive')
    try:
        result = discover(module.params['command_timeout'])
    except (OSError, ValueError) as exc:
        module.fail_json(msg='Cannot discover client mounts: {}'.format(exc))
    module.exit_json(**result)


if __name__ == '__main__':
    main()
