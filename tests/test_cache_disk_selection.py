"""Cache disk selection must count each filesystem once and skip runtime mounts."""
import builtins
import contextlib
import io
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[1]
TASKS = REPO / "roles/dingo_cache/tasks/main.yml"
TASK_NAME = "Measure mounts and select uniform cache disk set"
SUBDIR = "dingofs/dingo-cache-v5.2"
DISK_BYTES = 7_000_000_000_000  # ~6.4 TiB, above the 1000 GB floor


def _selection_source(mounts, exclude_prefixes, data_prefix, log_prefix):
    tasks = yaml.safe_load(TASKS.read_text())
    task = next(t for t in tasks if t.get("name") == TASK_NAME)
    rendered = jinja2.Template(task["shell"]).render(
        discovered_mounts={"stdout_lines": mounts},
        dingo_cache_min_disk_gb=1000,
        dingo_cache_disk_usage_ratio=0.5,
        dingo_cache_data_subdir=SUBDIR,
        dingo_cache_log_subdir="dingofs/logs/dingo-cache-v5.2",
        _dingo_cache_log_prefix=log_prefix,
        _dingo_cache_data_prefix=data_prefix,
        hostvars={"host": {}},
        inventory_hostname="host",
        dingo_cache_exclude_mount_prefixes=exclude_prefixes,
    )
    return rendered.split("<<'PYEOF'\n", 1)[1].rsplit("\nPYEOF", 1)[0]


def select(mounts, devices, exclude_prefixes=("/var/lib/kubelet/",),
           data_prefix="/mnt/disk", log_prefix="/mnt/aux", mountinfo=""):
    """Run the role's embedded selector against a synthetic filesystem table."""
    fixture = {path: (devices[path], DISK_BYTES) for path in mounts}

    def fake_stat(path):
        return types.SimpleNamespace(st_dev=fixture[str(path)][0])

    def fake_statvfs(path):
        return types.SimpleNamespace(f_blocks=fixture[str(path)][1], f_frsize=1)

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return io.StringIO(mountinfo)
        return real_open(path, *args, **kwargs)

    stream = io.StringIO()
    namespace = {"__name__": "fixture"}
    source = _selection_source(mounts, exclude_prefixes, data_prefix, log_prefix)
    with patch("os.stat", side_effect=fake_stat), \
            patch("os.statvfs", side_effect=fake_statvfs), \
            patch("builtins.open", side_effect=fake_open), \
            contextlib.redirect_stdout(stream):
        exec(compile(source, str(TASKS), "exec"), namespace)
    line = [row for row in stream.getvalue().splitlines() if row.startswith("JSON=")][-1]
    return json.loads(line[len("JSON="):])


class CacheDiskSelectionTests(unittest.TestCase):
    def test_kubelet_volume_subpaths_are_not_counted_as_disks(self):
        # One LVM volume is mounted at /mnt/disk2 AND at kubelet volume-subpath
        # mounts; only the canonical /mnt/disk2 mount may become a cache disk.
        disks = {"/mnt/disk%d" % i: i for i in range(1, 8)}
        kubelet = {
            "/var/lib/kubelet/pods/pod-1/volume-subpaths/rootcache-volume/main/%d" % i: 2
            for i in range(5, 10)
        }
        result = select(list(disks) + list(kubelet), {**disks, **kubelet})
        self.assertEqual(result["count"], 7)
        self.assertEqual(result["dirs"], ["/mnt/disk%d/%s" % (i, SUBDIR) for i in range(1, 8)])
        self.assertFalse(any("/var/lib/kubelet" in d for d in result["dirs"]))
        self.assertEqual(len(result["ignored"]), 5)

    def test_runtime_mount_is_excluded_even_when_it_is_the_only_mount(self):
        only = "/var/lib/kubelet/pods/pod-1/volume-subpaths/rootcache-volume/main/0"
        result = select(["/mnt/disk1", only], {"/mnt/disk1": 1, only: 2})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["dirs"], ["/mnt/disk1/" + SUBDIR])
        self.assertFalse(any("/var/lib/kubelet" in d for d in result["dirs"]))

    def test_same_filesystem_is_selected_once_preferring_data_prefix(self):
        mounts = ["/data/bind-of-disk2", "/mnt/disk2"]
        result = select(mounts, {"/data/bind-of-disk2": 2, "/mnt/disk2": 2})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["dirs"], ["/mnt/disk2/" + SUBDIR])
        self.assertTrue(any("/data/bind-of-disk2" in entry for entry in result["ignored"]))

    def test_null_exclude_prefixes_is_treated_as_empty(self):
        disks = {"/mnt/disk1": 1}
        result = select(["/mnt/disk1"], disks, exclude_prefixes=None)
        self.assertEqual(result["dirs"], ["/mnt/disk1/" + SUBDIR])

    def test_scalar_exclude_prefixes_fails_loudly(self):
        with self.assertRaises(SystemExit) as raised:
            select(["/mnt/disk1"], {"/mnt/disk1": 1},
                   exclude_prefixes="/var/lib/kubelet/")
        self.assertIn("must be a LIST", str(raised.exception))

    def test_empty_data_prefix_prefers_primary_mount_over_bind(self):
        # /mnt/disk2 is the filesystem's primary mount; /data/app binds a
        # subdirectory of it. With no data prefix the primary mount must win.
        mounts = ["/data/app", "/mnt/disk2"]
        mountinfo = (
            "36 25 259:2 / /mnt/disk2 rw,relatime - xfs /dev/mapper/dingo_cache_2-lv rw\n"
            "37 25 259:2 /app /data/app rw,relatime - xfs /dev/mapper/dingo_cache_2-lv rw\n"
        )
        result = select(mounts, {"/data/app": 2, "/mnt/disk2": 2},
                        data_prefix="", log_prefix="", mountinfo=mountinfo)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["dirs"], ["/mnt/disk2/" + SUBDIR])


if __name__ == "__main__":
    unittest.main()
