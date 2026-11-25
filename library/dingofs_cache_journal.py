#!/usr/bin/python
"""Count cache errors in a systemd unit's journal on the managed host."""
from collections import OrderedDict
from datetime import datetime
import json
import re
import subprocess


GLOG_ERROR = re.compile(
    r'^[EF](?:[0-9]{4}){1,2}\s+[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?\s+(?:[0-9]+\s+)?'
)
TEXT_ERROR = re.compile(r'^(?:error|fatal):', re.IGNORECASE)
INVOCATION_ID = re.compile(r'^[0-9a-f]{32}$')
NULL_INVOCATION_ID = '0' * 32


def summarize(lines):
    count = 0
    samples = OrderedDict()
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            raise ValueError(line[:512].decode('utf-8', 'replace').strip())
        message = entry.get('MESSAGE', '')
        if isinstance(message, list):
            message = bytes(message).decode('utf-8', 'replace')
        priority = entry.get('PRIORITY')
        glog = GLOG_ERROR.match(message)
        if not glog and priority not in ('0', '1', '2', '3') and not TEXT_ERROR.match(message):
            continue
        count += 1
        timestamp = datetime.fromtimestamp(int(entry['__REALTIME_TIMESTAMP']) / 1000000).astimezone()
        key = (message[0], message[glog.end():glog.end() + 512]) if glog else (priority, message[:512])
        samples[key] = '{} {}'.format(timestamp.isoformat(timespec='seconds'), ' '.join(message[:512].splitlines()))
        samples.move_to_end(key)
        if len(samples) > 3:
            samples.popitem(last=False)
    return count, list(samples.values())


def normalize_invocation_id(value):
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized or normalized == NULL_INVOCATION_ID:
        return None
    if not INVOCATION_ID.match(normalized):
        raise ValueError('invocation_id must be a 32-character hex string')
    return normalized


def resolve_lookback(hours=None, minutes=None):
    if hours is not None and minutes is not None:
        raise ValueError('lookback_hours and lookback_minutes are mutually exclusive')
    if minutes is not None:
        if minutes < 1:
            raise ValueError('lookback_minutes must be a positive integer')
        return '{} minutes ago'.format(minutes), '{}m'.format(minutes), None, minutes
    if hours is None:
        hours = 72
    if hours < 1:
        raise ValueError('lookback_hours must be a positive integer')
    return '{} hours ago'.format(hours), '{}h'.format(hours), hours, None


def read_journal(journalctl, unit, hours=None, minutes=None, invocation_id=None):
    since, _, _, _ = resolve_lookback(hours=hours, minutes=minutes)
    invocation = normalize_invocation_id(invocation_id)
    command = [journalctl, '--no-pager', '--output=json', '--all']
    if not invocation:
        command.extend(['--unit', unit])
    command.extend(['--since', since])
    if b'--output-fields' in subprocess.check_output([journalctl, '--help']):
        command.append('--output-fields=MESSAGE,PRIORITY')
    if invocation:
        command.extend([
            '_SYSTEMD_INVOCATION_ID={}'.format(invocation),
            '+',
            'INVOCATION_ID={}'.format(invocation),
        ])
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as journal:
        result = summarize(journal.stdout)
        if journal.wait() != 0:
            raise ValueError('journalctl exited with status {}'.format(journal.returncode))
    return result


def main():
    from ansible.module_utils.basic import AnsibleModule
    module = AnsibleModule(
        argument_spec=dict(
            unit=dict(type='str', required=True),
            lookback_hours=dict(type='int'),
            lookback_minutes=dict(type='int'),
            invocation_id=dict(type='str'),
        ),
        mutually_exclusive=[('lookback_hours', 'lookback_minutes')],
        supports_check_mode=True,
    )
    unit = module.params['unit']
    try:
        _, lookback_label, hours, minutes = resolve_lookback(
            hours=module.params['lookback_hours'],
            minutes=module.params['lookback_minutes'],
        )
        invocation = normalize_invocation_id(module.params['invocation_id'])
    except ValueError as exc:
        module.fail_json(msg=str(exc))
    journalctl = module.get_bin_path('journalctl', required=True)
    try:
        count, samples = read_journal(
            journalctl, unit, hours=hours, minutes=minutes, invocation_id=invocation
        )
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as exc:
        module.fail_json(msg='Cannot inspect the journal for {}: {}'.format(unit, exc))
    scope = 'current_invocation' if invocation else 'unit'
    window = (
        'current start, last {}'.format(lookback_label)
        if invocation else 'last {}'.format(lookback_label)
    )
    module.exit_json(
        changed=False,
        lookback_hours=hours,
        lookback_minutes=minutes,
        invocation_id=invocation or '',
        scope=scope,
        window=window,
        error_count=count,
        samples=samples,
    )


if __name__ == '__main__':
    main()
