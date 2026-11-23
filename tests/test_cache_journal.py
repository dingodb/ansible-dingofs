import json
from pathlib import Path
import tempfile
import unittest

from library.dingofs_cache_journal import read_journal, summarize


def entry(priority, message):
    return json.dumps({'__REALTIME_TIMESTAMP': '1795265796368411', 'PRIORITY': priority,
                       'MESSAGE': message}).encode()


class CacheJournalTests(unittest.TestCase):
    def test_glog_severity_survives_journal_info_priority(self):
        rdma = 'E20261121 20:56:36.368368 4116625 queue_pair.cc:123] Fail to modify queue pair to rtr: Invalid argument'
        repeat = rdma.replace('20:56:36.368368 4116625', '20:56:39.000002 4116622')
        count, samples = summarize([
            entry('6', 'W20261121 20:56:36.000001 1 rpc.cc:1] last error: timeout'),
            entry('6', 'I1121 20:56:36.000001 1 rpc.cc:1] errors=0'),
            entry('6', rdma), entry('6', 'F1121 20:56:38.000001 2 main.cc:5] exiting'),
            entry('3', repeat),
        ])
        self.assertEqual(count, 3)
        self.assertEqual(len(samples), 2)
        self.assertIn('F1121', samples[0])
        self.assertTrue(samples[1].endswith(repeat))

    def test_counts_all_errors_and_bounds_recent_samples(self):
        records = [entry('3', 'native error {}'.format(index)) for index in range(20)]
        records.append(entry('6', 'Error: Config file missing'))
        records.append(entry('2', list(b'large message ' + b'x' * 1000)))
        count, samples = summarize(records)
        self.assertEqual(count, 22)
        self.assertEqual(len(samples), 3)
        self.assertIn('native error 19', samples[0])
        self.assertIn('Config file missing', samples[1])
        self.assertIn('large message', samples[2])
        self.assertLess(len(samples[2]), 600)

    def test_quiet_unit_and_failed_journal_read(self):
        with tempfile.TemporaryDirectory() as directory:
            journalctl = Path(directory) / 'journalctl'
            journalctl.write_text('''#!/bin/sh
if [ "$1" = --help ]; then echo --output-fields; exit 0; fi
test "$*" = "--no-pager --output=json --all --unit dingo-cache-v5.2.service --since 1 hours ago --output-fields=MESSAGE,PRIORITY" || exit 2
''')
            journalctl.chmod(0o755)
            self.assertEqual(read_journal(str(journalctl), 'dingo-cache-v5.2.service', 1), (0, []))
            with self.assertRaisesRegex(ValueError, 'status 2'):
                read_journal(str(journalctl), 'dingo-cache-v5.2.service', 72)
            journalctl.write_text('#!/bin/sh\nif [ "$1" = --help ]; then exit 0; fi\necho "permission denied" >&2\nexit 1\n')
            with self.assertRaisesRegex(ValueError, 'permission denied'):
                read_journal(str(journalctl), 'dingo-cache-v5.2.service', 1)


if __name__ == '__main__':
    unittest.main()
