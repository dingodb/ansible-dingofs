"""Collect explicit client lifecycle events on the Ansible controller."""
from datetime import datetime, timezone
import csv
import io
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from ansible import context
from ansible.plugins.callback import CallbackBase


DOCUMENTATION = r"""
callback: dingofs_client_report
type: aggregate
short_description: Save skipped DingoFS client restarts on the controller
description:
  - Consumes explicit set_stats events from the DingoFS client roles.
  - Writes a private JSON report and skipped-instance TSV for each invocation.
  - DINGO_CLIENT_REPORT_DIR overrides the default reports directory in the current directory.
requirements:
  - Enable dingofs_client_report in callbacks_enabled.
"""

SKIPPED = frozenset(("skipped_busy_before_change", "restart_deferred_busy",
                     "restart_deferred_unknown"))
ERRORS = frozenset(("check_failed", "restart_failed", "verification_failed"))
STATUSES = SKIPPED | ERRORS | frozenset(("started", "completed", "dry_run"))
STAGES = frozenset(("precheck", "configure", "restart_check", "restart", "verify", "complete"))
FIELDS = ("host", "fsname", "mount_point", "service", "operation", "stage",
          "status", "reason", "changed_on_disk", "pending_restart", "task_failed")
CLIENT_PLAYBOOK = re.compile(r"(?:^|/)(?:client_04_deploy|client_07_upgrade|client_site)\.ya?ml(?::\d+)?$")


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_key(record):
    return tuple(record[key] for key in ("host", "fsname", "mount_point", "service", "operation"))


def tsv_value(value):
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "dingofs_client_report"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self):
        super().__init__()
        self._started_at = timestamp()
        self._run_id = "{}-{}-{}".format(
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"), os.getpid(), uuid.uuid4().hex[:10])
        self._report_root = Path(os.environ.get("DINGO_CLIENT_REPORT_DIR", "reports")).expanduser().absolute()
        self._run_directory = self._report_root / self._run_id
        self._json_path = self._run_directory / ("client-restart-report-" + self._run_id + ".json")
        self._tsv_path = self._run_directory / ("client-restart-skips-" + self._run_id + ".tsv")
        self._playbook = ""
        self._check_mode = bool(context.CLIARGS.get("check", False))
        self._relevant = False
        self._records = {}
        self._contexts = {}
        self._host_errors = {}
        self._ansible_stats = {}
        self._complete = False
        self._finished_at = None
        self._directory_ready = False
        self._write_warning = None

    @staticmethod
    def _private_result(result):
        data = getattr(result, "_result", {})
        task = getattr(result, "_task", None)
        return bool(data.get("_ansible_no_log") or data.get("no_log") or getattr(task, "no_log", False))

    def v2_playbook_on_start(self, playbook):
        self._playbook = str(getattr(playbook, "_file_name", ""))
        if CLIENT_PLAYBOOK.search(self._playbook):
            self._relevant = True
            self._flush()

    def v2_playbook_on_play_start(self, play):
        # Role metadata covers custom playbooks importing the standard roles;
        # the top-level filename also covers failures during gather_facts.
        try:
            names = [role.get_name() for role in play.get_roles()]
        except (AttributeError, TypeError):
            names = []
        try:
            path = play.get_path()
        except (AttributeError, TypeError):
            path = ""
        if (any(name.rsplit(".", 1)[-1] in ("dingo_client", "dingo_client_upgrade") for name in names)
                or CLIENT_PLAYBOOK.search(str(path))):
            self._relevant = True
            self._flush()

    def _event(self, result):
        if self._private_result(result):
            return
        data = getattr(result, "_result", {})
        stats = data.get("ansible_stats", {})
        if not isinstance(stats, dict) or not isinstance(stats.get("data"), dict):
            return
        event = stats["data"].get("dingofs_client_event")
        if event is None:
            return
        self._relevant = True
        if not self._valid_event(event):
            self._display.warning("Ignored a malformed DingoFS client report event; no task output was copied into the report.")
            self._flush()
            return
        host = result._host.get_name()
        # Deliberate allowlist: never serialize hostvars, raw stdout/stderr or
        # unrecognized event keys (which could include connection credentials).
        row = {key: event[key] for key in FIELDS if key not in ("host", "task_failed")}
        event_check_mode = event.get("check_mode", self._check_mode)
        self._check_mode = self._check_mode or event_check_mode
        row.update(host=host, task_failed=False, timestamp=timestamp(), check_mode=self._check_mode)
        if self._check_mode and row["status"] in SKIPPED:
            row["planned_status"] = row["status"]
            row.update(status="dry_run", reason="check_mode")
        key = record_key(row)
        if row["status"] == "started":
            self._contexts[host] = row
            if key not in self._records or self._records[key]["status"] == "started":
                self._records[key] = row.copy()
        else:
            self._records[key] = row.copy()
            if row["status"] in ERRORS:
                # Keep explicit errors available for the following fail/assert.
                self._contexts[host] = row
            else:
                self._contexts.pop(host, None)
        self._flush()

    @staticmethod
    def _valid_event(event):
        if not isinstance(event, dict) or event.get("schema_version") != 1:
            return False
        if event.get("operation") not in ("deploy", "upgrade"):
            return False
        if event.get("stage") not in STAGES or event.get("status") not in STATUSES:
            return False
        if "changed_on_disk" not in event:
            return False
        if event["changed_on_disk"] is not None and not isinstance(event["changed_on_disk"], bool):
            return False
        if event.get("pending_restart") is not None and not isinstance(event.get("pending_restart"), bool):
            return False
        if "pending_restart" not in event:
            return False
        if "check_mode" in event and not isinstance(event["check_mode"], bool):
            return False
        return all(isinstance(event.get(key), str) and len(event[key]) <= limit
                   for key, limit in (("fsname", 1024), ("mount_point", 4096),
                                      ("service", 1024), ("reason", 1024)))

    def v2_runner_on_ok(self, result):
        self._event(result)

    def v2_runner_item_on_ok(self, result):
        self._event(result)

    def _failure(self, result, unreachable=False, ignore_errors=False):
        if self._private_result(result):
            return
        data = getattr(result, "_result", {})
        host = result._host.get_name()
        current = self._contexts.pop(host, None)
        if current:
            row = current.copy()
            stage = row["stage"]
            rc = data.get("rc")
            if unreachable:
                status, reason = "check_failed", "Host became unreachable before this instance completed."
            elif (stage == "restart_check" and isinstance(rc, int) and not isinstance(rc, bool)
                  and rc == 1 and not data.get("skipped")
                  and not (self._check_mode or row.get("check_mode"))):
                status, reason = "restart_deferred_busy", "Mount became busy at the final restart check."
                row["pending_restart"] = True
            elif stage == "restart":
                status, reason = "restart_failed", "The client restart task failed."
            elif stage == "verify":
                status, reason = "verification_failed", "Client service or mount verification failed."
            else:
                status, reason = "check_failed", "Client checks or configuration failed before completion."
            # An explicit error event can give a more precise static reason.
            if row["status"] not in ERRORS:
                row.update(status=status, reason=reason)
            row.update(task_failed=True, ignored=bool(ignore_errors), timestamp=timestamp())
            self._records[record_key(row)] = row
        else:
            kind = "unreachable" if unreachable else "failed"
            self._host_errors[(host, kind)] = {
                "host": host, "fsname": None, "mount_point": None, "service": None,
                "status": kind, "reason": "Host failed outside a reported client instance.",
                "ignored": bool(ignore_errors), "timestamp": timestamp(),
            }
        self._flush()

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._failure(result, ignore_errors=ignore_errors)

    def v2_runner_on_unreachable(self, result):
        self._failure(result, unreachable=True)

    def v2_playbook_on_stats(self, stats):
        for host in sorted(stats.processed):
            summary = stats.summarize(host)
            self._ansible_stats[host] = {
                key: int(summary.get(key, 0))
                for key in ("ok", "changed", "skipped", "failures", "unreachable", "rescued", "ignored")
            }
        self._complete = True
        self._finished_at = timestamp()
        if self._relevant and self._flush():
            skipped = [r for r in self._records.values() if r["status"] in SKIPPED]
            self._display.display("DingoFS client report: {} skipped instance(s) on {} host(s).".format(
                len(skipped), len({row["host"] for row in skipped})))
            self._display.display("  JSON: {}".format(self._json_path))
            self._display.display("  TSV:  {}".format(self._tsv_path))
            self._display.display("  The TSV lists skips only; inspect JSON errors and failed/unreachable hosts before treating the run as successful.")

    def _snapshot(self):
        rows = [self._records[key].copy() for key in sorted(self._records)]
        return dict(
            schema_version=1, run_id=self._run_id, started_at=self._started_at,
            finished_at=self._finished_at, complete=self._complete, check_mode=self._check_mode,
            playbook=self._playbook, records=rows,
            skipped=[row for row in rows if row["status"] in SKIPPED],
            errors=[row for row in rows if row["status"] in ERRORS or row.get("task_failed")],
            host_errors=[self._host_errors[key].copy() for key in sorted(self._host_errors)],
            ansible_stats=self._ansible_stats,
            failed_hosts=[host for host, values in self._ansible_stats.items() if values["failures"]],
            unreachable_hosts=[host for host, values in self._ansible_stats.items() if values["unreachable"]],
        )

    @staticmethod
    def _atomic_write(path, text):
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, str(path))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _flush(self):
        if not self._relevant:
            return False
        try:
            if not self._directory_ready:
                self._report_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                # Do not chmod a user-supplied existing root such as /tmp.
                self._run_directory.mkdir(mode=0o700)
                self._directory_ready = True
            snapshot = self._snapshot()
            self._atomic_write(self._json_path, json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
            text = io.StringIO()
            writer = csv.writer(text, delimiter="\t", lineterminator="\n")
            writer.writerow(FIELDS)
            for row in snapshot["skipped"]:
                writer.writerow([tsv_value(row.get(key)) for key in FIELDS])
            self._atomic_write(self._tsv_path, text.getvalue())
            self._write_warning = None
            return True
        except (OSError, ValueError) as exc:
            warning = "Could not write DingoFS client report to {}: {}".format(self._run_directory, exc)
            if warning != self._write_warning:
                self._display.warning(warning)
                self._write_warning = warning
            return False
