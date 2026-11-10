"""Strict monitor status parsing: a failed/unknown probe is never absence."""
import re
from ansible.errors import AnsibleFilterError


def monitor_count(text):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(text))
    header, count = False, 0
    roles = {"node_exporter", "prometheus", "grafana", "monitor_sync"}
    for raw in text.splitlines():
        fields = raw.split()
        if not fields or set(raw.strip()) <= {"-", " "}:
            continue
        if re.match(r"^Id\s+Role\s+Host\s+Container Id\s+Status", raw.strip()):
            header = True
            continue
        if re.match(r"^(cluster name|cluster kind|grafana address)\s*:", raw.strip()):
            continue
        if len(fields) < 5 or not re.fullmatch(r"[0-9a-f]{6,}", fields[0]) or fields[1] not in roles:
            raise AnsibleFilterError("Unrecognized monitor status; refusing automatic deployment")
        if fields[4] != "Cleaned":
            count += 1
    if not header:
        raise AnsibleFilterError("Missing monitor status header; refusing automatic deployment")
    return count


class FilterModule:
    def filters(self):
        return {"dingo_monitor_count": monitor_count}
