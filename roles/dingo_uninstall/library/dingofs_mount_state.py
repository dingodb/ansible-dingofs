#!/usr/bin/python
"""Read exact mountpoint state without interpreting mountpoint(1) exit codes."""
import os
from pathlib import Path
import re

MOUNTINFO = Path('/proc/self/mountinfo')


def mount_state(target, source=MOUNTINFO):
    if not target.startswith('/') or os.path.normpath(target) != target or '\0' in target:
        raise ValueError('Expected a normalized absolute mount point')
    matches = []
    data = source.read_text()
    if not data.strip():
        raise ValueError('Empty kernel mount table: ' + str(source))
    for line in data.splitlines():
        fields = line.split()
        try:
            separator = fields.index('-')
        except ValueError:
            raise ValueError('Malformed kernel mount table: ' + str(source))
        if separator < 6 or len(fields) < separator + 4:
            raise ValueError('Malformed kernel mount table: ' + str(source))
        decode = lambda value: re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
        if decode(fields[4]) == target:
            matches.append(dict(device=fields[2], fstype=fields[separator + 1],
                                source=decode(fields[separator + 2])))
    # No stat() is required: absent paths and detached FUSE mounts are handled
    # without traversing a stale filesystem or mistaking its parent for it.
    return dict(changed=False, path=target, mounted=bool(matches), mounts=matches)


def main():
    from ansible.module_utils.basic import AnsibleModule
    module = AnsibleModule(argument_spec=dict(path=dict(type='path', required=True)),
                           supports_check_mode=True)
    try:
        result = mount_state(module.params['path'])
    except (OSError, ValueError) as exc:
        module.fail_json(msg='Cannot determine mount state: ' + str(exc))
    module.exit_json(**result)


if __name__ == '__main__':
    main()
