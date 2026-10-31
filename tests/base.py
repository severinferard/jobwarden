"""What the jobwarden tests share: python3 -m unittest discover -s tests -t .   (stdlib only, no network, no real GPU, nothing billable)

SSH is a stub, or a throwaway sshd on 127.0.0.1 standing in for a rented pod (custom
port, no docker, temporary); jobs live in a temp HOME, never in the real ~/.jobwarden/jobs.
"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
JOBRUN = os.path.join(ROOT, "jobwarden", "jobrun.py")
JOBWARDEN = [sys.executable, "-m", "jobwarden"]
sys.path.insert(0, ROOT)

HOOK = os.path.join(ROOT, "plugins/jobwarden/scripts/remind_waiter.py")
UUID_A, UUID_B = "GPU-aaaa", "GPU-bbbb"
HOSTSTATS = os.path.expanduser("~/Code/hoststats")


def load_jobrun():
    loader = importlib.machinery.SourceFileLoader("jobrun_mod", JOBRUN)
    spec = importlib.util.spec_from_loader("jobrun_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def write_exe(path, body):
    with open(path, "w") as f:
        f.write(textwrap.dedent(body).lstrip())
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def wait_for(fn, timeout=20, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def read_file(path):
    with open(path) as f:
        return f.read()


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Base(unittest.TestCase):
    """A fake host: its own HOME, and a PATH with a fake nvidia-smi whose answers are files the test edits."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jk")
        self.addCleanup(self.cleanup)
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.home), os.makedirs(self.bin)
        self.smi = os.path.join(self.tmp, "smi")
        os.makedirs(self.smi)
        self.set_gpus([(UUID_A, "NVIDIA GeForce RTX 2080 SUPER", 10), (UUID_B, "NVIDIA GeForce RTX 2060", 10)], [])
        write_exe(
            os.path.join(self.bin, "nvidia-smi"),
            f"""
            #!/bin/sh
            case "$*" in
              *compute-apps*) cat {self.smi}/apps ;;
              *power.draw*) cat {self.smi}/stats ;;
              *) cat {self.smi}/gpus ;;
            esac
        """,
        )
        self.env = dict(
            os.environ,
            HOME=self.home,
            PATH=self.bin + os.pathsep + os.environ["PATH"],
            JOBWARDEN_HEARTBEAT="0.2",
            JOBWARDEN_GPU_POLL="0.2",
            JOBWARDEN_HOSTS=os.path.join(self.home, ".config/jobwarden/hosts.json"),
            JOBWARDEN_SSH_DIR=os.path.join(self.tmp, "s"),
            JOBWARDEN_REPO=ROOT,
            PYTHONPATH=ROOT,
            HOSTSTATS_REPO=HOSTSTATS,
        )
        for k in ("CMUX_SOCKET_PATH", "CMUX_WORKSPACE_ID", "CMUX_SURFACE_ID"):
            self.env.pop(k, None)
        self.procs = []
        self.save_hosts({"lo": {"ssh": "local"}})

    def cleanup(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
            for stream in (p.stdout, p.stderr):
                if stream:
                    stream.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def save_hosts(self, hosts):
        os.makedirs(os.path.dirname(self.env["JOBWARDEN_HOSTS"]), exist_ok=True)
        with open(self.env["JOBWARDEN_HOSTS"], "w") as f:
            json.dump(hosts, f)

    def set_gpus(self, gpus, apps):
        with open(f"{self.smi}/gpus", "w") as f:
            f.writelines(f"{u}, {n}, {m}\n" for u, n, m in gpus)
        with open(f"{self.smi}/apps", "w") as f:
            f.writelines(f"{u}, {pid}, {n}, {m}\n" for u, pid, n, m in apps)
        with open(f"{self.smi}/stats", "w") as f:
            f.writelines(f"{i}, {u}, {n}, 97, {m}, 8192, 71, 100, 50\n" for i, (u, n, m) in enumerate(gpus))

    def hosts_file(self):
        with open(self.env["JOBWARDEN_HOSTS"]) as f:
            return json.load(f)

    def jobrun(self, *args, **kw):
        return subprocess.run(
            [sys.executable, JOBRUN, "start", *args], env=self.env, capture_output=True, text=True, timeout=30, **kw
        )

    def status(self, name):
        with open(os.path.join(self.home, ".jobwarden", "jobs", name, "status.json")) as f:
            return json.load(f)

    def wait_state(self, name, *states, timeout=20):
        return wait_for(lambda: (s := self.status(name))["state"] in states and s, timeout, f"{name} in {states}")

    def log(self, name):
        with open(os.path.join(self.home, ".jobwarden", "jobs", name, "log")) as f:
            return f.read()

    def dock(self, name):
        """The job as every view gets it, from `jobwarden status`."""
        r = subprocess.run(
            [*JOBWARDEN, "status", "--all", "--json"], env=self.env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return next((j for j in json.loads(r.stdout)["jobs"] if j["name"] == name), None)

    def watch_once(self, host, *args):
        return subprocess.run(
            [*JOBWARDEN, "watch", *args, "--host", host, "--once"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def watch(self, host, *args, interval="0.3"):
        p = subprocess.Popen(
            [*JOBWARDEN, "watch", *args, "--host", host, "--interval", interval],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(p)
        return p

    def waiter(self, *args):
        p = subprocess.Popen(
            [*JOBWARDEN, "wait", *args, "--interval", "0.2"],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.procs.append(p)
        return p
