#!/usr/bin/python
"""Controller-side discovery summaries and complete, private report artifacts."""
from collections import Counter
from datetime import datetime, timezone
import csv
import io
import json
import os
from pathlib import Path
import re
import tempfile


def clean(value):
    return re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', str(value))


def table(headers, rows, caps):
    rows = [[clean(cell) for cell in row] for row in rows]
    widths = [min(cap, max([len(header)] + [len(row[i]) for row in rows]))
              for i, (header, cap) in enumerate(zip(headers, caps))]
    def line(row):
        cells = [value if len(value) <= width else value[:width - 3] + '...'
                 for value, width in zip(row, widths)]
        return '  '.join(value.ljust(width) for value, width in zip(cells, widths)).rstrip()
    return '\n'.join([line(headers), line(['-' * width for width in widths])] + [line(row) for row in rows])


def host_issues(host):
    issues = []
    def add(kind, detail, instance=None):
        instance = instance or {}
        issues.append(dict(host=host['host'], category=kind, fsname=instance.get('fsname', ''),
                           mount_point=instance.get('mount_point', ''), service=instance.get('service', ''),
                           detail=detail))
    status = host.get('scan_status', 'failed')
    if status != 'ok':
        add('SCAN_' + status.upper(), host.get('error') or '; '.join(map(str, host.get('warnings', []))) or status)
    for service in host.get('services', []):
        active = service.get('active_state', 'unknown')
        mounted = service.get('mount_state', 'unknown')
        kind = {'different_filesystem': 'WRONG_FILESYSTEM', 'not_mounted': 'NOT_MOUNTED',
                'unknown': 'MOUNT_UNKNOWN'}.get(mounted)
        if not kind and active != 'active':
            kind = 'SERVICE_' + active.upper()
        if kind:
            add(kind, '{}: {} / {} ({})'.format(service['service'], active, mounted,
                                               service.get('mount_point') or '?'), service)
    mounts = {mount['mount_point'] for mount in host.get('mounts', [])}
    services = {service['service'] for service in host.get('services', [])}
    for process in host.get('processes', []):
        if process.get('mount_namespace') == 'other':
            add('OTHER_NAMESPACE', 'PID {}: {}'.format(process['pid'], process.get('mount_point') or '?'), process)
        elif process.get('mount_point') and process['mount_point'] not in mounts and process.get('service') not in services:
            add('UNMOUNTED_PROCESS', 'PID {}: {}'.format(process['pid'], process['mount_point']), process)
    return issues


def layout_key(host):
    states = {service['service']: service.get('active_state', 'unknown') for service in host.get('services', [])}
    return tuple(sorted((mount.get('fsname', ''), mount['mount_point'], mount.get('root', '/'),
                         mount.get('fstype', ''), mount.get('source', ''), mount.get('read_only', False),
                         tuple(sorted((name, states.get(name, 'unknown')) for name in mount.get('services', []))))
                        for mount in host.get('mounts', [])))


def build_report(hosts, limit=20, check_mode=False):
    hosts = sorted(hosts, key=lambda host: host['host'])
    statuses = Counter(host.get('scan_status', 'failed') for host in hosts)
    issues = [issue for host in hosts for issue in host_issues(host)]
    priority = {'SCAN_UNREACHABLE': 0, 'SCAN_FAILED': 1, 'SCAN_PARTIAL': 2, 'WRONG_FILESYSTEM': 3}
    issues.sort(key=lambda issue: (priority.get(issue['category'], 4), issue['host'], issue['service']))
    grouped = {}
    for host in hosts:
        # A failed/unreachable scan must never enter the "no mounts" layout.
        if host.get('scan_status') in ('ok', 'partial'):
            grouped.setdefault(layout_key(host), []).append(host['host'])
    layouts = []
    membership = {}
    for number, (key, names) in enumerate(sorted(grouped.items(), key=lambda item: (-len(item[1]), item[1][0])), 1):
        group = 'G{:02d}'.format(number)
        descriptions = []
        for mount in key:
            services = ','.join('{}:{}'.format(name[:-8] if name.endswith('.service') else name, state)
                                for name, state in mount[6]) or 'no service'
            description = '{}@{}'.format(mount[0] or mount[4] or '?', mount[1])
            if mount[2] != '/':
                description += ' root=' + mount[2]
            if mount[3] != 'fuse.dingofs':
                description += ' type=' + mount[3]
            descriptions.append(description + (' (ro)' if mount[5] else '') + ' [' + services + ']')
        description = ', '.join(descriptions) or '(no DingoFS mounts)'
        layouts.append(dict(group=group, nodes=len(names), hosts=names, layout=description))
        membership.update({name: group for name in names})
    totals = dict(nodes=len(hosts), ok=statuses['ok'], partial=statuses['partial'], failed=statuses['failed'],
                  unreachable=statuses['unreachable'], attention_hosts=len({issue['host'] for issue in issues}),
                  findings=len(issues), mounts=sum(len(host.get('mounts', [])) for host in hosts),
                  services=sum(len(host.get('services', [])) for host in hosts),
                  other_fuse_mounts=sum(len(host.get('other_fuse_mounts', [])) for host in hosts))
    lines = ['DingoFS client discovery',
             'Nodes: {nodes} | OK: {ok} | PARTIAL: {partial} | FAILED: {failed} | UNREACHABLE: {unreachable}'.format(**totals),
             'DingoFS mounts: {mounts} | Services: {services} | Other FUSE: {other_fuse_mounts} | Attention: {attention_hosts} node(s)'.format(**totals), '']
    if issues:
        lines.append('ATTENTION ({} finding(s); first {})'.format(len(issues), min(limit, len(issues))))
        lines.append(table(['HOST', 'FINDING', 'DETAILS'],
                           [[issue['host'], issue['category'], issue['detail']] for issue in issues[:limit]], [34, 20, 76]))
        if len(issues) > limit:
            lines.append('... {} more finding(s); see issues.tsv'.format(len(issues) - limit))
    else:
        lines.append('ATTENTION: none (mount presence only; I/O health was not probed)')
    lines.extend(['', 'MOUNT LAYOUTS ({} group(s); first {})'.format(len(layouts), min(limit, len(layouts)))])
    rows = []
    for layout in layouts[:limit]:
        examples = ', '.join(layout['hosts'][:2])
        if layout['nodes'] > 2:
            examples += ' (+{})'.format(layout['nodes'] - 2)
        rows.append([layout['group'], str(layout['nodes']), examples, layout['layout']])
    if rows:
        lines.append(table(['GROUP', 'NODES', 'EXAMPLE HOSTS', 'FILESYSTEM@MOUNTPOINT'], rows, [7, 7, 40, 90]))
    else:
        lines.append('No completed or partial scans; mount layouts are unknown.')
    if len(layouts) > limit:
        lines.append('... {} more group(s); see layouts.tsv'.format(len(layouts) - limit))
    lines.append('Full node membership: hosts.tsv | Mount details: mounts.tsv | Findings: issues.tsv')
    return dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                check_mode=check_mode, hosts=hosts, totals=totals, layouts=layouts,
                issues=issues, membership=membership, summary='\n'.join(lines))


def tsv(headers, rows):
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
    writer.writerow(headers)
    writer.writerows([[clean(value) for value in row] for row in rows])
    return stream.getvalue()


def write_report(report_dir, hosts, limit=20, check_mode=False):
    report = build_report(hosts, limit, check_mode)
    root = Path(report_dir).expanduser().absolute()
    # Existing controller directories retain their permissions.
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix='run-', dir=str(root)))
    counts = Counter(issue['host'] for issue in report['issues'])
    files = {'report.json': json.dumps(report, ensure_ascii=False, indent=2) + '\n',
             'summary.txt': report['summary'] + '\n',
             'hosts.tsv': tsv(['host', 'address', 'scan_status', 'group', 'mounts', 'services', 'other_fuse', 'findings'],
                              [[host['host'], host.get('address', ''), host.get('scan_status', 'failed'),
                                report['membership'].get(host['host'], '-'), len(host.get('mounts', [])),
                                len(host.get('services', [])), len(host.get('other_fuse_mounts', [])), counts[host['host']]]
                               for host in report['hosts']]),
             'mounts.tsv': tsv(['host', 'fsname', 'mount_point', 'root', 'fstype', 'source', 'read_only', 'services', 'pids'],
                               [[host['host'], mount.get('fsname', ''), mount['mount_point'], mount.get('root', '/'),
                                 mount.get('fstype', ''), mount.get('source', ''), mount.get('read_only', False),
                                 ','.join(mount.get('services', [])), ','.join(map(str, mount.get('pids', [])))]
                                for host in report['hosts'] for mount in host.get('mounts', [])]),
             'layouts.tsv': tsv(['group', 'nodes', 'hosts', 'layout'],
                                [[layout['group'], layout['nodes'], ','.join(layout['hosts']), layout['layout']]
                                 for layout in report['layouts']]),
             'issues.tsv': tsv(['host', 'category', 'fsname', 'mount_point', 'service', 'detail'],
                               [[issue[key] for key in ('host', 'category', 'fsname', 'mount_point', 'service', 'detail')]
                                for issue in report['issues']])}
    for name, content in files.items():
        fd = os.open(str(run / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
    return dict(changed=False, dingofs_discovery_report=True, summary=report['summary'], totals=report['totals'],
                report_directory=str(run), reports={name: str(run / name) for name in files})


def main():
    from ansible.module_utils.basic import AnsibleModule
    module = AnsibleModule(argument_spec=dict(hosts=dict(type='list', elements='dict', required=True),
                                             report_dir=dict(type='path', required=True),
                                             display_limit=dict(type='int', default=20)), supports_check_mode=True)
    if module.params['display_limit'] < 1:
        module.fail_json(msg='display_limit must be positive')
    try:
        result = write_report(module.params['report_dir'], module.params['hosts'],
                              module.params['display_limit'], module.check_mode)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        module.fail_json(msg='Cannot write discovery report: {}'.format(exc))
    module.exit_json(**result)


if __name__ == '__main__':
    main()
