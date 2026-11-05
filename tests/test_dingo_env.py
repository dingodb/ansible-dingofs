"""Exercise CLI installation and upgrades through the real Ansible role."""
import functools
import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest


REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("ansible-playbook"), "requires Ansible")
class DingoEnvTests(unittest.TestCase):
    def test_latest_is_downloaded_once_then_distributed_to_all_hosts(self):
        with tempfile.TemporaryDirectory(prefix="ansible-dingo-download-") as directory:
            root = Path(directory)
            web = root / "web"
            web.mkdir()
            latest = web / "dingo"
            latest.write_text("#!/bin/sh\nprintf '%s\\n' 'independent CLI one'\n")
            requests = []

            class Handler(http.server.SimpleHTTPRequestHandler):
                def do_GET(self):
                    requests.append(self.path)
                    super().do_GET()

                def log_message(self, *args):
                    pass

            server = http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), functools.partial(Handler, directory=str(web)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            source = root / "controller/dingo"
            server_source = root / "manual-server-dingo"
            server_content = "#!/bin/sh\nprintf '%s\\n' 'compatible server CLI'\n"
            server_source.write_text(server_content)
            homes = {name: root / name for name in ("node1", "node2")}
            targets = [home / ".dingo/bin/dingo" for home in homes.values()]
            fixture_bin = root / "bin"
            fixture_bin.mkdir()
            getent = fixture_bin / "getent"
            entries = ["%s:x:1000:1000::%s:/bin/sh" % (name, home)
                       for name, home in homes.items()]
            getent.write_text("#!/bin/sh\nprintf '%s\\n' " + " ".join(map(shlex.quote, entries)) + "\n")
            getent.chmod(0o755)
            for target in targets:
                target.parent.mkdir(parents=True)
                target.write_text("#!/bin/sh\nprintf 'old release CLI\\n'\n")
                target.chmod(0o755)
            play = [{
                "hosts": "all", "gather_facts": False, "become": False,
                "environment": {"PATH": str(fixture_bin) + ":" + os.environ["PATH"]},
                "vars": {
                    "ansible_connection": "local",
                    "ansible_python_interpreter": sys.executable,
                    "dingo_env_home": str(root) + "/{{ inventory_hostname }}",
                    "dingo_cli_latest_pkg_path": str(source),
                    "dingo_cli_pkg_path": str(server_source),
                    "dingo_cli_download_url": "http://127.0.0.1:%s/dingo" % server.server_port,
                    "dingo_cli_download_enabled": True,
                    "dingo_env_mdsaddr": "127.0.0.1:7400",
                },
                "roles": ["dingo_env"],
            }]
            play_path = root / "play.json"
            play_path.write_text(json.dumps(play))
            env = dict(os.environ, ANSIBLE_CONFIG=str(REPO / "ansible.cfg"),
                       ANSIBLE_ROLES_PATH=str(REPO / "roles"), ANSIBLE_NOCOLOR="1")

            def run(*options):
                result = subprocess.run(
                    ["ansible-playbook", "-i", "node1,node2,", str(play_path), *options],
                    cwd=REPO, env=env, text=True, capture_output=True, timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            run()
            self.assertEqual(requests, ["/dingo"])
            self.assertEqual(source.read_bytes(), latest.read_bytes())
            self.assertEqual(server_source.read_text(), server_content)
            for target in targets:
                self.assertEqual(target.read_bytes(), latest.read_bytes())
            mtimes = [target.stat().st_mtime_ns for target in targets]
            run()
            self.assertEqual(len(requests), 2)
            self.assertEqual([target.stat().st_mtime_ns for target in targets], mtimes)

            # Same URL and path, new independent release: refresh and distribute.
            latest.write_text(latest.read_text().replace("CLI one", "CLI two"))
            run()
            self.assertEqual(len(requests), 3)
            for target in targets:
                self.assertEqual(target.read_bytes(), latest.read_bytes())
            run("--check")
            self.assertEqual(len(requests), 3)
            self.assertEqual(server_source.read_text(), server_content)

            # An old/incorrect inventory alias must not let latest overwrite
            # the manually prepared server CLI, even through a symlink.
            alias = root / "server-alias"
            alias.symlink_to(server_source)
            play[0]["vars"]["dingo_cli_latest_pkg_path"] = str(alias)
            play_path.write_text(json.dumps(play))
            result = subprocess.run(
                ["ansible-playbook", "-i", "node1,node2,", str(play_path)],
                cwd=REPO, env=env, text=True, capture_output=True, timeout=60,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must differ", result.stdout)
            self.assertEqual(len(requests), 3)
            self.assertEqual(server_source.read_text(), server_content)

    def test_cache_binary_entry_preserves_config_and_admin_uses_manual_cli(self):
        with tempfile.TemporaryDirectory(prefix="ansible-dingo-modes-") as directory:
            root = Path(directory)
            latest = root / "latest"
            latest.write_text("#!/bin/sh\nprintf 'latest worker CLI\\n'\n")
            manual = root / "manual"
            manual.write_text("#!/bin/sh\nprintf 'compatible server CLI\\n'\n")
            homes = {name: root / name for name in ("cache", "admin")}
            for home in homes.values():
                (home / ".dingo").mkdir(parents=True)
                (home / ".dingo/dingo.yaml").write_text("preserve existing configuration\n")
            inventory = root / "inventory.json"
            inventory.write_text(json.dumps({"all": {"children": {
                "cache_servers": {"hosts": {"cache": {"dingo_cli_download_enabled": False}}},
                "meta_servers": {"hosts": {"admin": {}}},
                "admin": {"hosts": {"admin": {"dingo_cli_download_enabled": True}}},
            }}}))
            play = [{
                "hosts": "all", "gather_facts": False, "become": False,
                "vars": {
                    "ansible_connection": "local",
                    "ansible_python_interpreter": sys.executable,
                    "dingo_env_home": str(root) + "/{{ inventory_hostname }}",
                    "dingo_cli_latest_pkg_path": str(latest),
                    "dingo_cli_pkg_path": str(manual),
                    "dingo_cli_download_url": "http://127.0.0.1:1/must-not-be-used",
                    "dingofs_cluster_name": "v5.2-tikv",
                },
                "tasks": [{"include_role": {"name": "dingo_env", "tasks_from": "binary"}}],
            }]
            play_path = root / "play.json"
            play_path.write_text(json.dumps(play))
            env = dict(os.environ, ANSIBLE_CONFIG=str(REPO / "ansible.cfg"),
                       ANSIBLE_ROLES_PATH=str(REPO / "roles"))
            result = subprocess.run(
                ["ansible-playbook", "-i", str(inventory), str(play_path)],
                cwd=REPO, env=env, text=True, capture_output=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((homes["cache"] / ".dingo/bin/dingo").read_bytes(), latest.read_bytes())
            self.assertEqual((homes["admin"] / ".dingo/bin/server-v5.2-tikv/dingo").read_bytes(), manual.read_bytes())
            self.assertFalse((homes["admin"] / ".dingo/bin/dingo").exists())
            for home in homes.values():
                self.assertEqual((home / ".dingo/dingo.yaml").read_text(), "preserve existing configuration\n")

    def test_install_upgrade_and_unchanged_reruns(self):
        with tempfile.TemporaryDirectory(prefix="ansible-dingo-env-") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            source = root / "dingo"
            source.write_text("#!/bin/sh\nprintf '%s\\n' 'fixture version one'\n")
            target = home / ".dingo/bin/dingo"

            # Provide an NSS fixture so the full role can use an isolated home
            # without creating a system user or touching the real user's files.
            fixture_bin = root / "bin"
            fixture_bin.mkdir()
            getent = fixture_bin / "getent"
            passwd_entry = "fixture:x:1000:1000::%s:/bin/sh" % home
            getent.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(passwd_entry) + "\n")
            getent.chmod(0o755)
            play = [{
                "hosts": "localhost", "gather_facts": False, "become": False,
                "environment": {"PATH": str(fixture_bin) + ":" + os.environ["PATH"]},
                "vars": {
                    "ansible_connection": "local",
                    "ansible_python_interpreter": sys.executable,
                    "dingo_env_home": str(home),
                    "dingo_env_bin_src": str(source),
                    "dingo_env_mdsaddr": "127.0.0.1:7400",
                },
                "roles": ["dingo_env"],
            }]
            play_path = root / "play.json"
            play_path.write_text(json.dumps(play))
            env = dict(os.environ, ANSIBLE_CONFIG=str(REPO / "ansible.cfg"),
                       ANSIBLE_ROLES_PATH=str(REPO / "roles"), ANSIBLE_NOCOLOR="1")

            def run(*options):
                result = subprocess.run(
                    ["ansible-playbook", "-i", "localhost,", str(play_path), *options],
                    cwd=REPO, env=env, text=True, capture_output=True, timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            # First installation must copy before trying to execute the CLI.
            self.assertIn("fixture version one", run())
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            self.assertIn("127.0.0.1:7400", (home / ".dingo/dingo.yaml").read_text())

            installed = target.stat()
            self.assertRegex(run(), r"changed=0\b")
            self.assertEqual(target.stat().st_mtime_ns, installed.st_mtime_ns)

            # Change content while preserving size and mtime: existence and
            # metadata checks must not conceal an upgrade at the same path.
            original = source.stat()
            source.write_text(source.read_text().replace("version one", "version two"))
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertEqual(source.stat().st_size, original.st_size)
            self.assertIn("changed=1", run("--check"))
            self.assertIn("version one", target.read_text())
            self.assertIn("fixture version two", run())
            self.assertEqual(target.read_bytes(), source.read_bytes())
            upgraded = target.stat()
            self.assertRegex(run(), r"changed=0\b")
            self.assertEqual(target.stat().st_mtime_ns, upgraded.st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
