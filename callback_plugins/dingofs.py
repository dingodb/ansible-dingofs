"""Default Ansible output, with compact output for explicitly marked discovery plays."""
from ansible.plugins.callback.default import CallbackModule as DefaultCallback


# Keep result formatting options local: the result_format_callback fragment
# does not exist before ansible-core 2.13, so inheriting it prevents startup.
DOCUMENTATION = r'''
name: dingofs
type: stdout
short_description: Default output with compact DingoFS discovery summaries
description:
  - Ordinary plays use the built-in default callback.
  - Marked discovery plays print a bounded summary instead of per-host task output.
  - Verbosity greater than zero restores normal task output.
extends_documentation_fragment:
  - default_callback
options:
  result_format:
    description:
      - Task result format used by the default callback on ansible-core 2.13 and later.
      - Older versions retain their built-in JSON output.
    type: str
    default: json
    choices: [json, yaml]
    env:
      - name: ANSIBLE_CALLBACK_RESULT_FORMAT
    ini:
      - section: defaults
        key: callback_result_format
  pretty_results:
    description:
      - Configure result readability on ansible-core 2.13 and later.
      - An unset value follows the built-in default for the selected result format.
    type: bool
    default: null
    env:
      - name: ANSIBLE_CALLBACK_FORMAT_PRETTY
    ini:
      - section: defaults
        key: callback_format_pretty
'''


class CallbackModule(DefaultCallback):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stdout'
    CALLBACK_NAME = 'dingofs'

    def __init__(self):
        super().__init__()
        self._compact = False
        self._only_compact = None
        self._strict_failures = set()
        self._reports = 0

    def v2_playbook_on_play_start(self, play):
        self._compact = (play.vars.get('dingofs_discovery_compact_output') is True
                         and self._display.verbosity == 0)
        self._only_compact = self._compact if self._only_compact is None else self._only_compact and self._compact
        super().v2_playbook_on_play_start(play)
        if self._compact:
            self._display.display('Scanning nodes; the grouped summary will follow. Use -v for task details.')

    def v2_playbook_on_task_start(self, task, is_conditional):
        if self._compact:
            self._last_task_name = task.get_name().strip()
        else:
            super().v2_playbook_on_task_start(task, is_conditional)

    def v2_runner_on_ok(self, result):
        data = result._result
        if (data.get('dingofs_discovery_report') is True and not data.get('_ansible_no_log')
                and not result._task.no_log):
            self._reports += 1
            self._display.display('\n' + data['summary'])
            self._display.display('Reports: ' + data['report_directory'])
        # Collector warnings are retained in the report and summarized as
        # findings. Printing them again per node would defeat compact output.
        if not self._compact:
            super().v2_runner_on_ok(result)

    def v2_runner_on_skipped(self, result):
        if not self._compact:
            super().v2_runner_on_skipped(result)

    def v2_runner_on_unreachable(self, result):
        if not (self._compact and 'discovery_scan' in result._task.tags):
            super().v2_runner_on_unreachable(result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if self._compact and 'discovery_strict' in result._task.tags:
            self._strict_failures.add(result._host.get_name())
        else:
            super().v2_runner_on_failed(result, ignore_errors)

    def v2_playbook_on_stats(self, stats):
        if self._only_compact is not True:
            return super().v2_playbook_on_stats(stats)
        if self._strict_failures:
            self._display.display('Strict discovery failed for {} node(s); inspect the saved report.'.format(len(self._strict_failures)))
        failures = sum(stats.summarize(host).get('failures', 0) for host in stats.processed)
        if failures:
            self._display.display('Ansible task failures: {}'.format(failures))
        if not self._reports:
            self._display.display('No discovery report was produced; check the errors above.')
