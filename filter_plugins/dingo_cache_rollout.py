"""Normalize requested Cache batches for Ansible's play-level serial."""
import re

from ansible.errors import AnsibleFilterError
import yaml


BATCH_ERROR = 'Cache batch size must be a positive host count, a percentage from 1% to 100%, or a nonempty list of those values.'


def cache_batch_sizes(value):
    if isinstance(value, str) and value.lstrip().startswith('['):
        try:
            value = yaml.safe_load(value)
        except yaml.YAMLError as error:
            raise AnsibleFilterError(BATCH_ERROR) from error

    sizes = value if isinstance(value, list) else [value]
    if not sizes:
        raise AnsibleFilterError(BATCH_ERROR)

    batches = []
    for size in sizes:
        match = re.fullmatch(r'([0-9]+)(%?)', str(size).strip())
        if not match:
            raise AnsibleFilterError(BATCH_ERROR)
        amount, percent = int(match[1]), match[2]
        if amount == 0 or (percent and amount > 100):
            raise AnsibleFilterError(BATCH_ERROR)
        batches.append('{}%'.format(amount) if percent else amount)
    return batches


class FilterModule:
    def filters(self):
        return {'dingo_cache_batch_sizes': cache_batch_sizes}
