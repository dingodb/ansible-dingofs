"""Read-only parsing of dingo CLI output for additional-cluster deployment."""

import configparser
import re
from collections import Counter

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def clean(text):
    return ANSI.sub("", str(text)).replace("\r", "")


def cluster_list(text):
    names, current = [], ""
    for raw in clean(text).splitlines():
        line = raw.strip()
        if not line or line == "…":
            continue
        selected = line.startswith("*")
        name = line[1:].strip() if selected else line
        if not NAME.fullmatch(name) or name in names:
            raise ValueError("Unrecognised or duplicate row in dingo cluster list")
        names.append(name)
        if selected:
            if current:
                raise ValueError("Multiple default clusters in dingo cluster list")
            current = name
    return {"names": sorted(names), "current": current}


def hosts_list(text):
    hosts = {}
    for raw in clean(text).splitlines():
        fields = raw.split()
        if not fields or fields[0] == "Host" or set(fields[0]) == {"-"}:
            continue
        if len(fields) < 4 or not fields[3].isdigit():
            raise ValueError("Unrecognised row in dingo hosts list")
        name, address, user, port = fields[:4]
        if name in hosts:
            raise ValueError("Duplicate host registration: " + name)
        hosts[name] = {"address": address, "user": user, "port": int(port)}
    if not hosts:
        raise ValueError("No registered hosts found")
    return hosts


def cli_config(content, database_url, engine):
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read_string(content)
        actual_url = config.get("database", "url").strip().strip('"')
        actual_engine = config.get("defaults", "engine").strip().strip('"')
        auto_upgrade = config.getboolean("defaults", "auto_upgrade")
    except (configparser.Error, ValueError):
        return {"valid": False, "errors": ["missing_or_invalid_settings"]}
    # Never include configuration values: database URLs may contain credentials.
    errors = []
    if actual_url != database_url:
        errors.append("database_url")
    if actual_engine != engine:
        errors.append("container_engine")
    if auto_upgrade:
        errors.append("auto_upgrade_must_be_false")
    return {"valid": not errors, "errors": errors}


def cluster_plan(topology):
    if not isinstance(topology, dict) or topology.get("kind") != "dingofs":
        raise ValueError("An additional cluster requires a dingofs topology")
    variables = topology.get("global", {}).get("variable", {})
    offset = int(topology.get("global", {}).get("sequence_offset", 0))
    instances, ports, directories = {}, {}, {}

    for section, service in topology.items():
        if not section.endswith("_services"):
            continue
        role = section[:-9]
        for entry in service.get("deploy", []):
            host = str(entry["host"])
            for key, value in variables.items():
                host = host.replace("${" + key + "}", str(value))
            replicas = int(entry.get("replicas", 1))
            if "${" in host or replicas < 1:
                raise ValueError("Unresolved host or invalid replica count")
            counts = instances.setdefault(role, {})
            counts[host] = counts.get(host, 0) + replicas
            for sequence in range(replicas):
                substitutions = dict(variables)
                substitutions.update({
                    "service_role": role,
                    "service_replica_sequence": sequence,
                    "service_replica_sequence_with_offset": sequence + offset,
                })
                config = dict(topology.get("global", {}))
                config.update(service.get("config", {}))
                values = [config[key] for key in ("data_dir", "raft_dir", "log_dir", "mount.volumes")
                          if key in config]
                if "home" in variables:
                    values.append(variables["home"])
                for path in values:
                    path = str(path)
                    for key, value in substitutions.items():
                        path = path.replace("${" + key + "}", str(value))
                    if "${" in path or not path.startswith("/"):
                        raise ValueError("Unresolved or non-absolute service directory")
                    directories.setdefault(host, set()).add(path.rstrip("/"))
                for key in ("server.port", "raft.port", "port", "mysqlPort"):
                    if key not in service.get("config", {}):
                        continue
                    value = str(service["config"][key])
                    value = value.replace("${service_replica_sequence}", str(sequence))
                    value = value.replace("${service_replica_sequence_with_offset}", str(sequence + offset))
                    if not value.isdigit() or not 0 < int(value) < 65536:
                        raise ValueError("Invalid service port in " + section)
                    host_ports = ports.setdefault(host, [])
                    if int(value) in host_ports:
                        raise ValueError("Duplicate port on " + host + ": " + value)
                    host_ports.append(int(value))
    if not instances:
        raise ValueError("Topology contains no deployable services")
    return {
        "instances": instances,
        "ports": {host: sorted(values) for host, values in ports.items()},
        "directories": {host: sorted(values) for host, values in directories.items()},
    }


def plan_conflicts(new, old):
    ports, paths = [], []
    for host, values in new["ports"].items():
        ports.extend(host + ":" + str(port)
                     for port in set(values).intersection(old["ports"].get(host, [])))
    for host, values in new["directories"].items():
        for path in values:
            for previous in old["directories"].get(host, []):
                if path == previous or path.startswith(previous + "/") or previous.startswith(path + "/"):
                    paths.append(host + ": " + path + " overlaps " + previous)
    return {"ports": sorted(ports), "paths": sorted(set(paths)),
            "compatible": not ports and not paths}


def managed_directory(path, mounts, plan):
    known = {value for values in plan["directories"].values() for value in values}
    return path in known and any(path.startswith(mount.rstrip("/") + "/") for mount in mounts)


def cluster_status(text, plan, expected_cluster):
    output = clean(text)
    cluster = re.search(r"^cluster name\s*:\s*(\S+)\s*$", output, re.M)
    if not cluster or cluster.group(1) != expected_cluster:
        raise ValueError("Status output does not identify the requested cluster")
    rows = []
    pattern = re.compile(r"^\s*(\S+)\s+(\S+)\s+(\S+)\s+(\d+)/(\d+)\s+(\S+)\s+(.+)$")
    actual, unhealthy = Counter(), []
    for line in output.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        _, role, host, count, _, container, status = match.groups()
        actual[(role, host)] += int(count)
        rows.append(line.strip())
        if not (status.startswith("Up ") or status == "Up" or status.startswith("RUNNING")) or container == "-":
            unhealthy.append(role + "@" + host + ": " + status.split()[0])
    if not rows:
        raise ValueError("No service rows in cluster status; refusing to infer an empty cluster")
    expected = Counter({
        (role, host): count
        for role, hosts in plan["instances"].items()
        for host, count in hosts.items()
    })
    return {
        "healthy": actual == expected and not unhealthy,
        "unhealthy": unhealthy,
        "counts_match": actual == expected,
        "rows": len(rows),
    }


def tiup_health(text):
    rows = []
    for line in clean(text).splitlines():
        fields = line.split()
        if len(fields) >= 6 and ":" in fields[0] and "/" in fields[4]:
            rows.append((fields[0], fields[5]))
    unhealthy = [identity + ": " + status for identity, status in rows
                 if status.split("|")[0] != "Up"]
    return {"healthy": bool(rows) and not unhealthy,
            "components": len(rows), "unhealthy": unhealthy}


class FilterModule:
    def filters(self):
        return {
            "dingo_cluster_list": cluster_list,
            "dingo_hosts_list": hosts_list,
            "dingo_cli_config": cli_config,
            "dingo_cluster_plan": cluster_plan,
            "dingo_plan_conflicts": plan_conflicts,
            "dingo_managed_directory": managed_directory,
            "dingo_cluster_status": cluster_status,
            "tiup_cluster_health": tiup_health,
        }
