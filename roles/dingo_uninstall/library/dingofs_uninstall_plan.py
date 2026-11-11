#!/usr/bin/python
"""Read-only ownership and deletion planning for one managed DingoFS instance."""
import json
import os
from pathlib import Path
import re
import shlex

UNIT_DIR = Path('/etc/systemd/system')
STATE_DIR = Path('/var/lib/dingofs-uninstall')
MOUNTINFO = Path('/proc/self/mountinfo')
PROTECTED = {'/', '/root', '/home', '/tmp', '/var', '/var/lib', '/etc', '/usr',
             '/usr/local', '/mnt', '/opt', '/root/dingofs', '/root/dingofs/pkg',
             '/root/dingofs/dingo-client', '/root/dingofs/dingo-cache', '/root/.dingo',
             '/root/.ssh', '/root/.kube', '/var/lib/dingofs-uninstall'}


def below(path, parent):
    return path == parent or path.startswith(parent.rstrip('/') + '/')


def safe_path(value, mounted_root=False):
    if not isinstance(value, str) or not value.startswith('/') or os.path.normpath(value) != value:
        raise ValueError('Expected a normalized absolute path: ' + str(value))
    if value in PROTECTED or re.fullmatch(r'/mnt/(disk[0-9]+|aux)', value) or len(Path(value).parts) < 3:
        raise ValueError('Refusing broad/system directory: ' + value)
    # A verified kernel mount root can be a disconnected FUSE filesystem.
    # Resolve only its parent; traversing/stat-ing the mount itself raises
    # ENOTCONN. All deletion paths still use the full symlink check.
    probe = os.path.dirname(value) if mounted_root else value
    if os.path.realpath(probe) != probe:
        raise ValueError('Refusing a path containing a symlink: ' + value)
    return value


def flags(path):
    result = {}
    for line in path.read_text().splitlines():
        if not line.startswith('--') or '=' not in line:
            continue
        key, value = line[2:].split('=', 1)
        if key in result:
            raise ValueError('Duplicate flag in ' + str(path) + ': ' + key)
        result[key] = value.strip()
    return result


def script_vars(path):
    result = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r'(g_mnt|g_fsname|g_log|g_mdsaddr)="([^"\n]*)"', line.strip())
        if match:
            result[match[1]] = match[2]
    return result


def unit_start(path):
    commands = [line.split('=', 1)[1].strip() for line in path.read_text().splitlines()
                if line.startswith('ExecStart=') and line.split('=', 1)[1].strip()]
    if len(commands) != 1:
        raise ValueError('Ambiguous ExecStart in ' + str(path))
    argv = shlex.split(commands[0])
    if argv and argv[0] in ('/bin/bash', '/usr/bin/bash', '/bin/sh', '/usr/bin/sh'):
        argv = argv[1:]
    if len(argv) != 1 or not argv[0].startswith('/'):
        raise ValueError('Unsupported managed ExecStart in ' + str(path))
    return argv[0]


def mount_rows(path):
    rows = []
    for line in path.read_text().splitlines():
        parts = line.split(); sep = parts.index('-')
        decode = lambda v: re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), v)
        rows.append({'path': decode(parts[4]), 'fstype': parts[sep + 1], 'source': decode(parts[sep + 2])})
    return rows


def make_plan(p, unit_dir=UNIT_DIR, state_dir=STATE_DIR, mounts=None):
    mounts = mount_rows(MOUNTINFO) if mounts is None else mounts
    service = p['service'][:-8] if p['service'].endswith('.service') else p['service']
    if not re.fullmatch(r'(dingo-cache(?:-[A-Za-z0-9_.@-]+)?|dingofs-mount-[A-Za-z0-9_.@-]+)', service):
        raise ValueError('Expected an explicitly named DingoFS instance service')
    base = safe_path(p['base_dir'])
    if str(state_dir) != str(STATE_DIR):
        safe_path(str(state_dir))
    expected_prefix = 'dingo-cache' if p['kind'] == 'cache' else 'dingofs-mount-'
    if not service.startswith(expected_prefix):
        raise ValueError('Service name does not match the selected component')
    if os.path.realpath(str(state_dir)) != str(state_dir):
        raise ValueError('Refusing a symlinked uninstall state directory')
    unit = unit_dir / (service + '.service')
    state_file = state_dir / (service + '.json')
    if unit.is_symlink() or state_file.is_symlink():
        raise ValueError('Refusing symlinked service/state file')
    script_name = 'dingo-cache-start.sh' if p['kind'] == 'cache' else 'dingofs-mount.sh'
    if unit.exists() and unit_start(unit) != base + '/' + script_name:
        raise ValueError('Service ExecStart does not belong to the configured installation')
    previous = json.loads(state_file.read_text()) if state_file.exists() else {}
    if previous and (previous.get('service') != service or previous.get('base_dir') != base or previous.get('kind') != p['kind']):
        raise ValueError('Saved uninstall ownership does not match this inventory')
    metadata = dict(previous)
    metadata.update(schema_version=1, kind=p['kind'], service=service, base_dir=base)
    source = Path(base) / ('dingo-cache.conf' if p['kind'] == 'cache' else 'dingofs-mount.sh')
    if source.is_symlink():
        raise ValueError('Refusing symlinked deployed configuration')
    if source.exists():
        if p['kind'] == 'cache':
            config = flags(source)
            required = ['cache_dir', 'log_dir', 'group_name', 'mds_addrs', 'id', 'listen_ip', 'listen_port']
            if any(not config.get(key) for key in required):
                raise ValueError('Deployed cache configuration is missing ownership fields')
            metadata.update(data_dirs=config['cache_dir'].split(','), log_dirs=[config['log_dir']],
                            group=config['group_name'], mds_addrs=config['mds_addrs'],
                            member_id=config['id'], listen_ip=config['listen_ip'], port=int(config['listen_port']))
            if not 1 <= metadata['port'] <= 65535:
                raise ValueError('Invalid deployed cache port')
        else:
            config = script_vars(source)
            if any(not config.get(key) for key in ['g_mnt', 'g_fsname', 'g_log', 'g_mdsaddr']):
                raise ValueError('Deployed client script is missing ownership fields')
            metadata.update(mount_point=config['g_mnt'], fsname=config['g_fsname'],
                            mds_addrs=config['g_mdsaddr'], data_dirs=[], log_dirs=[config['g_log']])
        if unit.exists() or not previous:
            metadata['unregistered'] = False
    elif unit.exists() or (Path(base).exists() and not previous):
        raise ValueError('Installation exists without a deployed ownership configuration')
    known = source.exists() or bool(previous)
    if p['kind'] == 'client':
        if not p['fsname']:
            raise ValueError('An explicit client filesystem name is required')
        attached = [row for row in mounts if row['path'] == p['mount_point']]
        if attached and any(not known or row['fstype'] not in ('fuse', 'fuse.dingofs')
                            or row['source'] != 'DingoFS:' + p['fsname'] for row in attached):
            raise ValueError('Mount ownership is unknown or belongs to another filesystem')
        wanted = safe_path(p['mount_point'], mounted_root=bool(attached))
        if known and (metadata.get('mount_point') != wanted or metadata.get('fsname') != p['fsname']):
            raise ValueError('Deployed filesystem/mount point differs from the selected instance')
        metadata.update(mount_point=wanted, fsname=p['fsname'])
    if known and p.get('mds_addrs') and set(metadata['mds_addrs'].split(',')) != set(p['mds_addrs'].split(',')):
        raise ValueError('Deployed MDS addresses differ from the selected inventory')
    if known and p['kind'] == 'cache' and p.get('group') and metadata['group'] != p['group']:
        raise ValueError('Deployed cache group differs from the selected inventory')
    data = [safe_path(v) for v in metadata.get('data_dirs', [])]
    logs = [safe_path(v) for v in metadata.get('log_dirs', [])]
    delete = ([base] if p['remove_installation'] and known else [])
    delete += data if p['remove_data'] and p['kind'] == 'cache' else []
    delete += logs if p['remove_logs'] else []
    delete = sorted(set(delete))
    retained = ([] if p['remove_data'] else data) + ([] if p['remove_logs'] else logs)
    for path in delete:
        if below(str(state_dir), path) or below(path, str(state_dir)):
            raise ValueError('Deletion overlaps uninstall ownership records: ' + path)
        if any(below(keep, path) or below(path, keep) for keep in retained):
            raise ValueError('Deletion overlaps retained data/logs; set remove_installation=false: ' + path)
    for path in delete:
        if any(below(row['path'], path) for row in mounts):
            raise ValueError('Refusing to recursively delete a mounted tree: ' + path)
    mounted = False
    if p['kind'] == 'client':
        for row in mounts:
            if row['path'] == metadata['mount_point']:
                mounted = True
                if not known or row['fstype'] not in ('fuse', 'fuse.dingofs') or row['source'] != 'DingoFS:' + p['fsname']:
                    raise ValueError('Mount ownership is unknown or belongs to another filesystem')
    # Check other managed services before removing shared directories.
    for other in unit_dir.glob('dingo*.service'):
        if other == unit:
            continue
        try:
            start = unit_start(other)
        except ValueError:
            # Unknown units cannot own a recognized installation script.
            if any(path in other.read_text() for path in delete):
                raise ValueError('Deletion is referenced by another service: ' + str(other))
            continue
        owned = [str(Path(start).parent)]
        if Path(start).name == 'dingo-cache-start.sh':
            conf = Path(start).parent / 'dingo-cache.conf'
            if conf.exists():
                c = flags(conf); owned += c.get('cache_dir', '').split(',') + [c.get('log_dir', '')]
        elif Path(start).name == 'dingofs-mount.sh' and Path(start).exists():
            c = script_vars(Path(start)); owned += [c.get('g_mnt', ''), c.get('g_log', '')]
            if p['kind'] == 'client' and c.get('g_mnt') == metadata['mount_point']:
                raise ValueError('Mount point is also owned by another service: ' + str(other))
        for path in delete:
            if any(v and (below(v, path) or below(path, v)) for v in owned):
                raise ValueError('Deletion overlaps another service: ' + str(other) + ': ' + path)
    remove_paths = [v for v in delete if Path(v).exists()]
    unregister = bool(p['kind'] == 'cache' and known and p['unregister'] and not metadata.get('unregistered', False))
    remove_mount_dir = bool(p['kind'] == 'client' and known and p.get('remove_mountpoint', True)
                            and (mounted or Path(metadata['mount_point']).is_dir()))
    return dict(changed=False, service=service, unit_path=str(unit), unit_exists=unit.exists(),
                base_dir=base, mount_point=metadata.get('mount_point', ''), mounted=mounted,
                metadata=metadata, state_file=str(state_file), remove_paths=remove_paths,
                retained_paths=retained, unregister=unregister,
                noop=not unit.exists() and not mounted and not remove_paths and not unregister and not remove_mount_dir)


def main():
    from ansible.module_utils.basic import AnsibleModule
    m = AnsibleModule(argument_spec=dict(
        kind=dict(type='str', choices=['cache', 'client'], required=True),
        service=dict(type='str', required=True), base_dir=dict(type='path', required=True),
        mount_point=dict(type='path', default=''), fsname=dict(type='str', default=''),
        mds_addrs=dict(type='str', default=''), group=dict(type='str', default=''),
        remove_installation=dict(type='bool', default=True), remove_data=dict(type='bool', default=False),
        remove_logs=dict(type='bool', default=False), unregister=dict(type='bool', default=True),
        remove_mountpoint=dict(type='bool', default=True),
        state_dir=dict(type='path', default=str(STATE_DIR))), supports_check_mode=True)
    try:
        plan = make_plan(m.params, state_dir=Path(m.params['state_dir']))
        rc, out, err = m.run_command(['systemctl', 'show', plan['service'] + '.service', '--property=LoadState,FragmentPath,ActiveState'])
        if rc:
            raise ValueError('Cannot inspect systemd service: ' + err)
        state = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
        fragment = state.get('FragmentPath', '')
        if fragment and fragment != plan['unit_path']:
            raise ValueError('Service is loaded from an unmanaged unit: ' + fragment)
        if not plan['unit_exists'] and state.get('ActiveState') in ('active', 'activating', 'deactivating'):
            raise ValueError('A live service has no verified managed unit file')
        plan['active_state'] = state.get('ActiveState', 'unknown')
        m.exit_json(**plan)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        m.fail_json(msg=str(exc))


if __name__ == '__main__':
    main()
