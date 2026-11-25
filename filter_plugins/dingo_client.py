"""Filter and validate DingoFS client mount selections (enabled + dingo_client_only)."""

from collections.abc import Mapping

try:
    from ansible.errors import AnsibleFilterError
except ImportError:  # pragma: no cover - standalone Python unit tests
    class AnsibleFilterError(ValueError):
        pass

try:
    from jinja2.runtime import Undefined as _JinjaUndefined
except ImportError:  # pragma: no cover - standalone Python unit tests
    _JinjaUndefined = ()


_TRUE_STRINGS = frozenset(("true", "yes", "on", "1"))
_FALSE_STRINGS = frozenset(("false", "no", "off", "0"))


def _is_undefined(value):
    return isinstance(value, _JinjaUndefined)


def _norm_mount_point(path):
    if path is None or _is_undefined(path):
        return ""
    text = str(path).strip()
    if not text:
        return ""
    stripped = text.rstrip("/")
    return stripped if stripped else "/"


def _effective_service_name(entry, version_suffix):
    explicit = entry.get("service_name")
    if explicit is not None and not _is_undefined(explicit):
        text = str(explicit).strip()
        if text:
            return text
    return "dingofs-mount-" + str(entry["fsname"]).strip() + version_suffix


def _parse_enabled(entry, index):
    if "enabled" not in entry:
        return True
    raw = entry["enabled"]
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    fsname = entry.get("fsname", "<index %d>" % index)
    raise AnsibleFilterError(
        "Invalid 'enabled' value %r for dingo_clients entry '%s' (expected boolean true/false)."
        % (raw, fsname)
    )


def parse_only(only):
    """Normalize dingo_client_only into an ordered deduplicated list of selectors."""
    if only is None or _is_undefined(only):
        return []
    if isinstance(only, bool):
        raise AnsibleFilterError(
            "dingo_client_only must be a comma-separated string or list of names, got boolean %r."
            % (only,)
        )
    if isinstance(only, str):
        stripped = only.strip()
        if not stripped:
            return []
        raw_items = stripped.split(",")
    elif isinstance(only, (list, tuple)):
        raw_items = list(only)
    else:
        raise AnsibleFilterError(
            "dingo_client_only must be a comma-separated string or list of names, got %s."
            % type(only).__name__
        )

    selectors = []
    seen = set()
    for item in raw_items:
        if not isinstance(item, str) or isinstance(item, bool):
            raise AnsibleFilterError(
                "dingo_client_only entries must be non-empty strings, got %r." % (item,)
            )
        token = item.strip()
        if not token:
            raise AnsibleFilterError(
                "dingo_client_only contains an empty selector: %r." % (only,)
            )
        if token not in seen:
            seen.add(token)
            selectors.append(token)
    return selectors


def _validate_clients(clients, host=None):
    prefix = "Host '%s': " % host if host else ""
    if _is_undefined(clients) or not isinstance(clients, (list, tuple)):
        raise AnsibleFilterError(
            "%sdingo_clients must be a list, got %s." % (prefix, type(clients).__name__)
        )
    validated = []
    for index, entry in enumerate(clients):
        if not isinstance(entry, Mapping):
            raise AnsibleFilterError(
                "%sdingo_clients[%d] must be a mapping, got %s."
                % (prefix, index, type(entry).__name__)
            )
        fsname = entry.get("fsname")
        if _is_undefined(fsname) or not isinstance(fsname, str) or not fsname.strip():
            raise AnsibleFilterError(
                "%sdingo_clients[%d] must define a non-empty string 'fsname'." % (prefix, index)
            )
        enabled = _parse_enabled(entry, index)
        validated.append((dict(entry), enabled))
    return validated


def _entry_matches_selector(entry, selector, version_suffix):
    if selector.startswith("/"):
        entry_mount = _norm_mount_point(entry.get("mount_point", ""))
        return bool(entry_mount) and _norm_mount_point(selector) == entry_mount
    fsname = str(entry["fsname"]).strip()
    if selector == fsname:
        return True
    service_name = _effective_service_name(entry, version_suffix)
    if selector == service_name:
        return True
    if selector.endswith(".service") and selector[:-8] == service_name:
        return True
    return False


def select_clients(clients, only=None, version_suffix="", include_disabled=False):
    """Filter a single host's dingo_clients list by `enabled` and `dingo_client_only`."""
    suffix = "" if (version_suffix is None or _is_undefined(version_suffix)) else str(version_suffix)
    selectors = parse_only(only)
    validated = _validate_clients(clients)

    all_entries = [entry for entry, _ in validated]
    enabled_entries = [entry for entry, is_enabled in validated if is_enabled]
    disabled_entries = [entry for entry, is_enabled in validated if not is_enabled]

    pool = all_entries if include_disabled else enabled_entries
    if not selectors:
        selected = list(pool)
        matched_selectors = []
        matched_disabled_selectors = []
        skipped_disabled = []
    else:
        selected = []
        matched_set = set()
        for entry in pool:
            matched_here = [s for s in selectors if _entry_matches_selector(entry, s, suffix)]
            if matched_here:
                selected.append(entry)
                matched_set.update(matched_here)
        matched_selectors = [s for s in selectors if s in matched_set]

        matched_disabled_set = set()
        skipped_disabled = []
        for entry in disabled_entries:
            matched_here = [s for s in selectors if _entry_matches_selector(entry, s, suffix)]
            if matched_here:
                matched_disabled_set.update(matched_here)
                if not include_disabled:
                    skipped_disabled.append(entry)
        matched_disabled_selectors = [s for s in selectors if s in matched_disabled_set]

    return {
        "selectors": selectors,
        "all": all_entries,
        "enabled": enabled_entries,
        "disabled": disabled_entries,
        "selected": selected,
        "matched_selectors": matched_selectors,
        "matched_disabled_selectors": matched_disabled_selectors,
        "skipped_disabled": skipped_disabled,
    }


def _hv_has(hv, key):
    if key not in hv:
        return False
    try:
        return not _is_undefined(hv[key])
    except KeyError:
        return False

def _resolve_host_clients(host, hv, play_clients, default_client):
    if _hv_has(hv, "dingo_clients"):
        return hv["dingo_clients"]
    if play_clients is not None and not _is_undefined(play_clients):
        return play_clients
    if default_client is not None and not _is_undefined(default_client):
        item = dict(default_client)
        if _hv_has(hv, "dingo_client_fsname"):
            item["fsname"] = hv["dingo_client_fsname"]
        if _hv_has(hv, "dingo_client_mount_point"):
            item["mount_point"] = hv["dingo_client_mount_point"]
        return [item]
    raise AnsibleFilterError("Host '%s' does not define dingo_clients." % host)


def validate_play(
    hosts,
    hostvars,
    only=None,
    include_disabled=False,
    play_clients=None,
    default_client=None,
    default_version_suffix="",
):
    """Validate dingo_client_only across every target host in the play (serial-safe)."""
    selectors = parse_only(only)
    fallback_suffix = (
        "" if (default_version_suffix is None or _is_undefined(default_version_suffix))
        else str(default_version_suffix)
    )

    enabled_hits = {s: 0 for s in selectors}
    disabled_hits = {s: 0 for s in selectors}
    available_enabled = []
    available_disabled = []
    seen_enabled = set()
    seen_disabled = set()
    selected_count = 0

    for host in list(hosts or []):
        hv = hostvars[host]
        raw_clients = _resolve_host_clients(host, hv, play_clients, default_client)
        suffix = hv["dingo_client_version_suffix"] if _hv_has(hv, "dingo_client_version_suffix") else fallback_suffix
        suffix = "" if (suffix is None or _is_undefined(suffix)) else str(suffix)
        # Validate with host context in error messages before calling select_clients.
        _validate_clients(raw_clients, host=host)
        selection = select_clients(
            raw_clients,
            only=selectors,
            version_suffix=suffix,
            include_disabled=include_disabled,
        )
        selected_count += len(selection["selected"])

        for entry in selection["enabled"]:
            fsname = str(entry["fsname"]).strip()
            if fsname not in seen_enabled:
                seen_enabled.add(fsname)
                available_enabled.append(fsname)
        for entry in selection["disabled"]:
            fsname = str(entry["fsname"]).strip()
            if fsname not in seen_disabled:
                seen_disabled.add(fsname)
                available_disabled.append(fsname)

        for selector in selectors:
            if any(_entry_matches_selector(e, selector, suffix) for e in selection["enabled"]):
                enabled_hits[selector] += 1
            if any(_entry_matches_selector(e, selector, suffix) for e in selection["disabled"]):
                disabled_hits[selector] += 1

    errors = []
    disabled_only = []
    unmatched = []
    if selectors:
        for selector in selectors:
            if include_disabled:
                if enabled_hits[selector] + disabled_hits[selector] == 0:
                    unmatched.append(selector)
            else:
                if enabled_hits[selector] == 0:
                    if disabled_hits[selector] > 0:
                        disabled_only.append(selector)
                    else:
                        unmatched.append(selector)

    if disabled_only:
        errors.append(
            "dingo_client_only matched only disabled client(s) (enabled: false): %s. "
            "Set enabled: true in inventory before deploy/upgrade/status, or use client_08_uninstall.yml to remove them."
            % ", ".join(disabled_only)
        )
    if unmatched:
        if include_disabled:
            available = available_enabled + [f + " (disabled)" for f in available_disabled if f not in seen_enabled]
        else:
            available = available_enabled
        available_hint = ", ".join(available) if available else "<none>"
        errors.append(
            "dingo_client_only matched no configured client on any host in this play: %s "
            "(available fsnames: %s; selectors match fsname, /mount_point, or service_name)."
            % (", ".join(unmatched), available_hint)
        )

    return {
        "valid": not errors,
        "error": " ".join(errors),
        "selectors": selectors,
        "selected_count": selected_count,
        "disabled_only": disabled_only,
        "unmatched": unmatched,
        "available_enabled": available_enabled,
        "available_disabled": available_disabled,
    }


class FilterModule:
    def filters(self):
        return {
            "dingo_client_select": select_clients,
            "dingo_client_validate_play": validate_play,
        }
