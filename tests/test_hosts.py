import os
import shutil
import subprocess
import unittest

from jobwarden import core
from unittest import mock

from tests.base import JOBWARDEN, JOBRUN, UUID_A, UUID_B, Base


class Hosts(Base):
    SAMPLE = {
        "box1": {
            "ssh": "user@10.0.0.2",
            "jump": "root@10.0.0.1",
            "extra": "-o HostKeyAlias=10.0.0.3",
            "gpus": {"2080": UUID_A, "2060": UUID_B},
        },
        "pod": {"ssh": "root@1.2.3.4", "port": 22022, "temporary": True, "extra": '-i "a b"'},
    }

    def test_round_trip_and_missing_file(self):
        with mock.patch.dict(os.environ, {"JOBWARDEN_HOSTS": os.path.join(self.tmp, "h.json")}):
            self.assertEqual(core.load_hosts(), {})
            core.save_hosts(self.SAMPLE)
            self.assertEqual(core.load_hosts(), self.SAMPLE)

    def test_ssh_command_has_route_and_robust_options(self):
        t = core.Transport("box1", self.SAMPLE["box1"])
        joined = " ".join(t.base)
        for want in ("BatchMode=yes", "ServerAliveInterval=10", "-J root@10.0.0.1", "HostKeyAlias=10.0.0.3", "-S "):
            self.assertIn(want, joined)
        self.assertIn("-p 22022", " ".join(core.Transport("pod", self.SAMPLE["pod"]).base))

    def test_socket_path_fits_macos_limit(self):
        path = core.socket_path("a-very-long-host-label-indeed", {"ssh": "u@h"}).replace(
            core.sockets_dir(), os.path.expanduser("~/.cache/jobwarden/ssh")
        )
        self.assertLess(len(path), 104 - 20)  # ssh appends a temporary suffix while binding

    def test_host_add_rm_cli(self):
        def jk(*a):
            return subprocess.run([*JOBWARDEN, *a], env=self.env, capture_output=True, text=True)

        self.assertEqual(
            jk("host", "add", "p1", "--ssh", "root@9.9.9.9", "--port", "2222", "--temporary").returncode, 0
        )
        h = self.hosts_file()["p1"]
        self.assertEqual((h["port"], h["temporary"]), (2222, True))
        self.assertIn("accept-new", h["extra"])
        self.assertIn("p1", jk("host", "list").stdout)
        self.assertEqual(jk("host", "rm", "p1").returncode, 0)
        self.assertNotIn("p1", self.hosts_file())
        self.assertNotEqual(jk("host", "rm", "p1").returncode, 0)

    def test_hosts_belong_to_the_project(self):
        env = {k: v for k, v in self.env.items() if k != "JOBWARDEN_HOSTS"}
        project, other = os.path.join(self.tmp, "project"), os.path.join(self.tmp, "other")
        deep = os.path.join(project, "a", "b")
        os.makedirs(deep), os.makedirs(other)

        def jk(cwd, *a):
            return subprocess.run([*JOBWARDEN, *a], cwd=cwd, env=env, capture_output=True, text=True)

        self.assertEqual(jk(project, "host", "add", "projecthost", "--ssh", "u@h").returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(project, ".jobwarden", "hosts.json")))
        self.assertIn("projecthost", jk(deep, "host", "list").stdout)
        self.assertNotIn("projecthost", jk(other, "host", "list").stdout)

    def test_one_label_on_two_routes_is_two_connections(self):
        self.assertNotEqual(core.socket_path("box", {"ssh": "u@a"}), core.socket_path("box", {"ssh": "u@b"}))

    def test_unknown_host(self):
        r = self.watch_once("ghost", "j")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown host", r.stderr)


class Python310(Base):
    """The whole toolkit is meant for Python 3.10+."""

    @unittest.skipUnless(shutil.which("python3.10"), "no python3.10")
    def test_runs_on_310(self):
        py = shutil.which("python3.10")
        r = subprocess.run([py, JOBRUN, "start", "p310", "--", "exit 3"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.wait_state("p310", "failed")["exit_code"], 3)
        r = subprocess.run(
            [py, "-m", "jobwarden", "watch", "p310", "--host", "lo", "--once"],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("exit code 3", r.stdout)
