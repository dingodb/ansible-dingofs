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


def read_journal(journalctl, unit, hours):
    command = [journalctl, '--no-pager', '--output=json', '--all', '--unit', unit,
               '--since', '{} hours ago'.format(hours)]
    if b'--output-fields' in subprocess.check_output([journalctl, '--help']):
        command.append('--output-fields=MESSAGE,PRIORITY')
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as journal:
        result = summarize(journal.stdout)
        if journal.wait() != 0:
            raise ValueError('journalctl exited with status {}'.format(journal.returncode))
    return result


def main():
    from ansible.module_utils.basic import AnsibleModule
    module = AnsibleModule(argument_spec=dict(unit=dict(type='str', required=True),
                                              lookback_hours=dict(type='int', default=72)),
                           supports_check_mode=True)
    hours = module.params['lookback_hours']
    if hours < 1:
        module.fail_json(msg='lookback_hours must be a positive integer')
    unit = module.params['unit']
    journalctl = module.get_bin_path('journalctl', required=True)
    try:
        count, samples = read_journal(journalctl, unit, hours)
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as exc:
        module.fail_json(msg='Cannot inspect the journal for {}: {}'.format(unit, exc))
    module.exit_json(changed=False, lookback_hours=hours, error_count=count, samples=samples)


if __name__ == '__main__':
    main()
