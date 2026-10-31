"""The controlling machine's side of jobwarden: a project's hosts file and one robust way to run a command on a host (stdlib only, Python 3.10+)."""

import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time

_TIMEOUT = os.environ.get("JOBWARDEN_SSH_TIMEOUT")  # the tests shorten all three
CONNECT_TIMEOUT_S = float(_TIMEOUT or 15)
MASTER_TIMEOUT_S = float(_TIMEOUT or 40)
COMMAND_TIMEOUT_S = float(_TIMEOUT or 30)
PERSIST_S = 300
# After failed connects, wait before the next one: some networks block a source for a while after a burst of new SSH
# connections. Shared by every reader of the host through a file.
BACKOFF_S = [float(x) for x in os.environ.get("JOBWARDEN_BACKOFF", "15,30,60,120").split(",")]


LOCAL = "local"


class Unreachable(Exception):
    pass


class UnknownHost(Exception):
    pass


def config_path():
    """Hosts belong to a project: the nearest .jobwarden/hosts.json from here upwards, else a new one here."""
    if os.environ.get("JOBWARDEN_HOSTS"):
        return os.path.expanduser(os.environ["JOBWARDEN_HOSTS"])
    here = os.getcwd()
    while True:
        path = os.path.join(here, ".jobwarden", "hosts.json")
        if os.path.exists(path):
            return path
        if os.path.dirname(here) == here:
            return os.path.join(os.getcwd(), ".jobwarden", "hosts.json")
        here = os.path.dirname(here)


def has_hosts_file():
    return os.path.exists(config_path())


def sockets_dir():
    return os.path.expanduser(os.environ.get("JOBWARDEN_SSH_DIR", "~/.cache/jobwarden/ssh"))


def socket_path(label, settings):
    """Named after the route too: two projects can use one label for different hosts, and one route is one connection."""
    route = hashlib.sha256(f"{settings.get('ssh')}|{settings.get('port')}|{settings.get('jump')}".encode()).hexdigest()[
        :8
    ]
    return os.path.join(sockets_dir(), re.sub(r"[^\w.-]", "_", label)[:20] + "-" + route)


def is_local(settings):
    return settings.get("ssh") == "local"


def load_hosts():
    try:
        with open(config_path()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_hosts(hosts):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(hosts, f, indent=2)
        f.write("\n")
    os.replace(path + ".tmp", path)


def get_host(name):
    """A host's settings. "local" (this machine) needs no entry."""
    host = load_hosts().get(name) or ({"ssh": "local"} if name == LOCAL else None)
    if host is None:
        raise UnknownHost(
            f"unknown host {name!r} in {config_path()}: {', '.join(load_hosts()) or 'none yet'} (see `jobwarden host add`)"
        )
    return host


def route_args(settings):
    """The ssh options of a settings's route, without its address."""
    args = shlex.split(settings.get("extra", ""))
    if settings.get("port"):
        args += ["-p", str(settings["port"])]
    if settings.get("settings_key_alias"):
        args += ["-o", f"HostKeyAlias={settings['settings_key_alias']}"]
    if settings.get("jump"):
        args += ["-J", settings["jump"]]
    return args


def run_bounded(argv, timeout, stdin_text=None, env=None):
    """(rc, stdout, stderr), rc None on timeout. The whole process group is killed: killing ssh alone orphans ssh -J's
    proxy child, which keeps a half-open connection on the host. Output goes through temp files, not pipes, so nothing
    that outlives the call can hold it open (a `communicate()` after the kill would wait for it)."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as inp:
        if stdin_text:
            inp.write(stdin_text.encode())
            inp.seek(0)
        p = subprocess.Popen(argv, stdin=inp, stdout=out, stderr=err, start_new_session=True, env=env)
        try:
            p.wait(timeout)
            rc = p.returncode
        except BaseException as e:  # the timeout, or the caller being interrupted: leave nothing behind
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                pass  # unkillable for now (stuck in the kernel); not worth freezing for
            if not isinstance(e, subprocess.TimeoutExpired):
                raise
            rc = None
        out.seek(0), err.seek(0)
        return rc, out.read().decode(errors="replace"), err.read().decode(errors="replace")


class Transport:
    """Commands on one host over one shared connection (ControlMaster), each with a hard, process-group timeout."""

    def __init__(self, label, settings, eager=False):
        """eager: someone is asking right now, so don't make them sit out the backoff."""
        self.eager = eager
        self.label, self.settings = label, settings
        self.local = is_local(settings)
        os.makedirs(sockets_dir(), mode=0o700, exist_ok=True)
        self.socket = socket_path(label, settings)
        self.base = (
            ["ssh"]
            + route_args(settings)
            + [
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={max(CONNECT_TIMEOUT_S, 1):.0f}",
                "-o",
                "ServerAliveInterval=10",
                "-o",
                "ServerAliveCountMax=3",
                "-S",
                self.socket,
                "-o",
                "SetEnv=LC_ALL=C.UTF-8",
            ]
        )  # or ssh forwards this machine's locale, which the hosts may not have
        self.target = settings.get("ssh", "")

    def _backoff_left(self):
        """(seconds left to wait, failures so far) from the host's shared attempt file."""
        try:
            with open(self.socket + ".fails") as f:
                fails = int(f.read().strip() or 0)
            mtime = os.path.getmtime(self.socket + ".fails")
        except (OSError, ValueError):
            return 0, 0
        wait = BACKOFF_S[min(fails, len(BACKOFF_S)) - 1] if fails else 0
        return wait - (time.time() - mtime), fails

    def _note_attempt(self, fails):
        tmp = f"{self.socket}.fails.{os.getpid()}"
        with open(tmp, "w") as f:
            f.write(str(fails))
        os.replace(tmp, self.socket + ".fails")

    def _master_ok(self):
        return os.path.exists(self.socket) and run_bounded(self.base + ["-O", "check", self.target], 5)[0] == 0

    def connect(self):
        """Make sure the shared connection is up; raises Unreachable. Only one watcher at a time opens it."""
        if self.local or self._master_ok():
            return
        lock = os.open(self.socket + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            limit = time.time() + MASTER_TIMEOUT_S + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() > limit:
                        raise Unreachable("another watcher is still connecting") from None
                    time.sleep(0.5)
            if self._master_ok():
                return
            left, fails = self._backoff_left()
            if left > 0 and not self.eager:
                raise Unreachable(f"last connection attempt failed, next in {left:.0f} s")
            self.drop()
            self._note_attempt(fails + 1)  # counted before trying: a killed watcher must not reset the backoff
            # Own stdio files and -f: a master started on the side by a command would keep that command's pipes open.
            rc, _, err = run_bounded(
                self.base + ["-M", "-N", "-f", "-o", f"ControlPersist={PERSIST_S}", self.target], MASTER_TIMEOUT_S
            )
            if rc != 0:
                raise Unreachable(
                    err.strip().splitlines()[-1]
                    if rc and err.strip()
                    else f"no connection after {MASTER_TIMEOUT_S:.0f} s"
                    if rc is None
                    else f"ssh exit {rc}"
                )
        finally:
            os.close(lock)

    def drop(self):
        """Forget the shared connection. A wedged master ends by itself through ServerAlive (~30 s)."""
        if os.path.exists(self.socket):
            run_bounded(self.base + ["-O", "exit", self.target], 5)
            try:
                os.unlink(self.socket)
            except OSError:
                pass

    def run(self, command, stdin_text=None, timeout=None):
        """Run a command string; returns (rc, stdout, stderr), raises Unreachable. A failure leaves the shared connection
        alone: others may be using it, and if it is really dead ServerAlive ends it, then connect() applies the backoff."""
        timeout = timeout or COMMAND_TIMEOUT_S
        if self.local:
            rc, out, err = run_bounded(["sh", "-c", command], timeout, stdin_text)
            if rc is None:
                raise Unreachable(f"local command timed out after {timeout:.0f} s")
            return rc, out, err
        self.connect()
        rc, out, err = run_bounded(self.base + ["-o", "ControlMaster=no", self.target, command], timeout, stdin_text)
        if rc is None:
            raise Unreachable(f"no answer after {timeout:.0f} s")
        if rc == 255:
            raise Unreachable(err.strip().splitlines()[-1] if err.strip() else "ssh failed")
        try:
            os.unlink(self.socket + ".fails")  # only a command that worked resets the backoff, not a connection alone
        except FileNotFoundError:
            pass
        return rc, out, err


def side_command(label, settings):
    """ssh command for reading a host on the side (a stats tool). Uses the shared connection when one is up; otherwise leaves
    ~/.ssh/config in charge, which may keep its own ControlMaster for routes that punish new connections."""
    if is_local(settings):
        return "local"
    parts = ["ssh"] + route_args(settings) + ["-o", "ConnectTimeout=8", "-o", "BatchMode=yes"]
    sock = socket_path(label, settings)
    if os.path.exists(sock):
        parts += ["-S", sock, "-o", "ControlMaster=no"]
    return shlex.join(parts + [settings["ssh"]])


def ssh_command(label):
    """An ssh command for reading a host, e.g. with hoststats. It opens the host's shared connection first, so that a stats tool
    polling every few seconds never makes a new connection; raises Unreachable if the host doesn't answer."""
    settings = get_host(label)
    Transport(label, settings).connect()
    return side_command(label, settings)
