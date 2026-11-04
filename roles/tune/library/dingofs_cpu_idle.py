#!/usr/bin/python
"""Apply the runtime CPUIdle policy without fixed state indexes or cpupower."""

from pathlib import Path
import re


DOCUMENTATION = r"""
module: dingofs_cpu_idle
short_description: Disable C1E and idle states above an exit-latency threshold
description:
  - Inspect every online CPU's CPUIdle sysfs states before making changes.
  - Disable C1E by name regardless of its state index or exit latency.
  - Never re-enable previously disabled states. Verify writes by reading back.
  - This changes runtime OS policy, not BIOS settings or reboot persistence.
options:
  latency_threshold_us:
    description: Disable states whose exit latency is at least this many microseconds.
    type: int
    default: 3
  disable_c1e:
    description: Also disable states named C1E or a C1E name with a driver suffix.
    type: bool
    default: true
attributes:
  check_mode:
    support: full
"""

EXAMPLES = r"""
- name: Disable C1E, including its 2 us variants
  dingofs_cpu_idle:
    latency_threshold_us: 3
    disable_c1e: true
"""

CPU_SYSFS_ROOT = "/sys/devices/system/cpu"


class IdleError(Exception):
    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


def online_cpus(root):
    cpus = []
    for cpu in sorted(root.glob("cpu[0-9]*")):
        if not re.fullmatch(r"cpu[0-9]+", cpu.name):
            continue
        online = cpu / "online"
        # CPU0 and non-hotpluggable CPUs may have no online attribute.
        value = online.read_text().strip() if online.exists() else "1"
        if value not in ("0", "1"):
            raise ValueError("Unexpected CPU online value at {}: {!r}".format(online, value))
        if value == "1":
            cpus.append(cpu)
    return cpus


def configure(root, latency_threshold_us=3, disable_c1e=True, check_mode=False):
    result = dict(changed=False, online_cpus=0, states_examined=0,
                  target_states=0, c1e_states=0, states_changed=[],
                  states_to_change=[], cpus_without_cpuidle=[], warnings=[])
    try:
        if latency_threshold_us < 0:
            raise ValueError("latency_threshold_us must be non-negative")
        root = Path(root)
        cpus = online_cpus(root)
        result["online_cpus"] = len(cpus)
        pending = []
        targets = []
        # Read and validate the complete snapshot before writing any state.
        for cpu in cpus:
            states = sorted(p for p in (cpu / "cpuidle").glob("state[0-9]*")
                            if re.fullmatch(r"state[0-9]+", p.name))
            if not states:
                result["cpus_without_cpuidle"].append(cpu.name)
            for state in states:
                name = (state / "name").read_text().strip()
                raw_latency = (state / "latency").read_text().strip()
                if not raw_latency.isdecimal():
                    raise ValueError("Invalid CPUIdle latency at {}: {!r}".format(state, raw_latency))
                latency = int(raw_latency)
                if not name or latency < 0:
                    raise ValueError("Invalid CPUIdle name/latency at {}".format(state))
                c1e = bool(re.match(r"^C1E(?:$|[-_])", name.upper()))
                result["states_examined"] += 1
                result["c1e_states"] += int(c1e)
                if not ((disable_c1e and c1e) or latency >= latency_threshold_us):
                    continue
                path = state / "disable"
                value = path.read_text().strip()
                if value not in ("0", "1"):
                    raise ValueError("Unexpected disable value at {}: {!r}".format(path, value))
                targets.append(path)
                if value != "1":
                    pending.append(path)
        result["target_states"] = len(targets)
        result["states_to_change"] = [str(path) for path in pending]
        if not cpus:
            result["warnings"].append("No online CPU sysfs interfaces found; CPU idle policy was not verified.")
        elif result["cpus_without_cpuidle"]:
            result["warnings"].append("No CPUIdle states exposed on: {}. Their idle policy was not verified."
                                      .format(", ".join(result["cpus_without_cpuidle"])))
        if disable_c1e and not result["c1e_states"]:
            result["warnings"].append("No named C1E state is exposed. This does not prove firmware C1E is disabled.")
        if check_mode:
            result["changed"] = bool(pending)
            return result
        for path in pending:
            path.write_text("1\n")
            result["changed"] = True
            result["states_changed"].append(str(path))
            if path.read_text().strip() != "1":
                raise ValueError("CPUIdle disable write did not persist at {}".format(path))
        # Include states that were already disabled, in case a policy daemon reset them.
        for path in targets:
            if path.read_text().strip() != "1":
                raise ValueError("CPUIdle state is still enabled at {}".format(path))
        if online_cpus(root) != cpus:
            raise ValueError("Online CPU set changed during tuning; rerun to cover the current CPUs")
    except (OSError, ValueError) as exc:
        raise IdleError(str(exc), result) from exc
    return result


def main():
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(
        argument_spec=dict(latency_threshold_us=dict(type="int", default=3),
                           disable_c1e=dict(type="bool", default=True)),
        supports_check_mode=True,
    )
    try:
        result = configure(CPU_SYSFS_ROOT, check_mode=module.check_mode, **module.params)
    except IdleError as exc:
        module.fail_json(msg=str(exc), **exc.result)
    module.exit_json(**result)


if __name__ == "__main__":
    main()
