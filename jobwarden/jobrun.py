#!/usr/bin/env python3
"""Run a long job detached on this host and record what happened to it (single file, stdlib only, Python 3.10+).

    jobrun start <name> [--gpu SHORT_NAME|any|GPU-uuid] [--wait-gpu] [--force] [--cwd DIR] [--summary TEXT] [--project NAME]
                 [--min-runtime SECONDS] [--expect FILE]... -- <cmd>
    jobrun stop <name> [--grace SECONDS]   stop that job only: SIGTERM, then SIGKILL after the grace (default 30)
    jobrun adopt <name>                    follow a running job whose supervisor has died, so its progress is tracked again
    jobrun discover-gpus                   write ~/.jobwarden/gpus.json from nvidia-smi and print it

One argument after `--` is a shell string; several are run as they are, without a shell. The job survives SSH
disconnects. It writes to ~/.jobwarden/jobs/<name>/log; ~/.jobwarden/jobs/<name>/status.json holds start, pids, heartbeat, GPU, the last
progress line and, at the end, the exit code. A previous run of the same name moves to ~/.jobwarden/jobs/<name>/prev/<time>/.

Progress lines, parsed here and nowhere else (the word is free: step, turn, scene...):
    HH:MM:SS step 120/513 loss 0.85 1.2s/step
    HH:MM:SS stage gguf              names the current stage, counts as progress

--gpu pins the job to one card by UUID: plain processes get CUDA_VISIBLE_DEVICES, docker commands must use $JOB_GPU
themselves (`docker run --gpus "device=$JOB_GPU"`, quote the whole command so it reaches jobrun unexpanded); the command
is never rewritten. A busy card is refused (--wait-gpu queues, --force overrides). The card stays reserved by a lock
while the job lives, so two jobruns can't pick the same card before either has loaded its model. No --gpu = no GPU.

A docker command must name its container `--name "$JOB_CONTAINER"`, or a stop couldn't reach it: killing the docker client leaves
the container running. A docker command started by a script the job runs can't be checked, so name it there too.

A job counts as failed on a non-zero exit, when it ran less than --min-runtime, or when an --expect file (relative to
the job's cwd) is missing, empty or older than the start. A job ended by `jobrun stop` counts as stopped. Exit code of jobrun
itself: 0 started, queued or stopped, 1 no such job or not stoppable, 2 bad usage, 4 name in use, 5 GPU busy or unknown.
"""

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

JOBS = os.path.expanduser("~/.jobwarden/jobs")
GPU_FILE = os.path.expanduser("~/.jobwarden/gpus.json")
FREE_MIB = 500  # a card using more than this counts as busy
GPU_POLL_S = float(os.environ.get("JOBWARDEN_GPU_POLL", 15))
HEARTBEAT_S = float(os.environ.get("JOBWARDEN_HEARTBEAT", 5))
TAIL_MAX = 1 << 20
EXIT_NAME, EXIT_GPU = 4, 5

CLOCK = r"^\s*\[?(\d\d:\d\d:\d\d)\]?\s+"
PROGRESS = re.compile(CLOCK + r"([A-Za-z_][\w-]*)\s+(\d+)\s*/\s*(\d+)\b(.*)$")
STAGE = re.compile(CLOCK + r"stage[ :=]+(.+?)\s*$")
METRIC = re.compile(r"([A-Za-z_][\w.]*)[ =:]+(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?%?|-?nan|-?inf)(?![\w/.])", re.I)
SPEED = re.compile(r"(\d+(?:\.\d+)?)\s*s/[A-Za-z]+")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
DOCKER_RUN = re.compile(r"\bdocker\s+(?:container\s+)?run\b[^|;&]*")
DETACHED = re.compile(r"\s(?:--detach\b|-[it]*d[it]*(?=\s|$))")
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def parse_progress(line):
    """{clock, unit, step, total, metrics, s_per_step} or {clock, stage} from a line, None if it is neither."""
    line = ANSI.sub("", line)
    m = STAGE.match(line)
    if m:
        return {"clock": m[1], "stage": m[2]}
    m = PROGRESS.match(line)
    if not m:
        return None
    speed = SPEED.search(m[5])
    return {
        "clock": m[1],
        "unit": m[2],
        "step": int(m[3]),
        "total": int(m[4]),
        "metrics": dict(METRIC.findall(m[5])),
        "s_per_step": float(speed[1]) if speed else None,
    }


def run_bounded(argv, timeout):
    """(rc, stdout, stderr); output goes through temp files and the whole group is killed on timeout, so nothing a
    child leaves behind can hold the call open. rc None on timeout or a missing binary."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        except OSError as e:
            return None, "", str(e)
        try:
            p.wait(timeout)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            rc = None
        out.seek(0), err.seek(0)
        return rc, out.read().decode(errors="replace"), err.read().decode(errors="replace")


def query_gpus():
    """Cards as nvidia-smi sees them; raises RuntimeError if it can't say."""
    rc, out, err = run_bounded(["nvidia-smi", "--query-gpu=uuid,name,memory.used", "--format=csv,noheader,nounits"], 20)
    if rc != 0:
        raise RuntimeError(f"nvidia-smi failed: {err.strip() or 'timeout or not installed'}")
    gpus = []
    for line in out.strip().splitlines():
        uuid, name, used = [x.strip() for x in line.split(",")[:3]]
        gpus.append({"uuid": uuid, "name": name, "used": float(used) if used.replace(".", "").isdigit() else 0.0})
    return gpus


def query_procs():
    rc, out, err = run_bounded(
        ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory", "--format=csv,noheader,nounits"],
        20,
    )
    if rc != 0:
        raise RuntimeError(f"nvidia-smi failed: {err.strip() or 'timeout or not installed'}")
    procs = []
    for line in out.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 4:
            procs.append({"uuid": parts[0], "pid": parts[1], "name": ",".join(parts[2:-1]), "mem": parts[-1]})
    return procs


def short_names(gpus):
    """{"2080": uuid, ...}: the model number, or the last word of the name; repeats get -0, -1."""
    bases = []
    for g in gpus:
        m = re.search(r"\b[A-Za-z]?(\d{3,5})\b", g["name"])
        bases.append((m[0] if m else g["name"].split()[-1]).lower())
    out, seen = {}, {}
    for base, g in zip(bases, gpus):
        key = base if bases.count(base) == 1 else f"{base}-{seen.get(base, 0)}"
        seen[base] = seen.get(base, 0) + 1
        out[key] = g["uuid"]
    return out


def discover():
    names = short_names(query_gpus())
    os.makedirs(os.path.dirname(GPU_FILE), exist_ok=True)
    write_json(GPU_FILE, names)
    return names


def known_gpus():
    try:
        with open(GPU_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return discover()


def resolve_gpu(wanted):
    """[(short name, uuid), ...] to try, in order; raises RuntimeError on an unknown name."""
    names = known_gpus()
    if wanted == "any":
        if not names:
            raise RuntimeError("no GPU on this host")
        return list(names.items())
    if wanted in names:
        return [(wanted, names[wanted])]
    for k, v in names.items():
        if v == wanted:
            return [(k, v)]
    raise RuntimeError(f"unknown GPU {wanted!r}, this host has: {', '.join(names) or 'none'}")


def lock_path(uuid):
    d = os.path.join(JOBS, ".gpu")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, uuid + ".lock")


def try_gpu(uuid):
    """(lock fd, None) if the card is ours now, else (None, why it is busy). The fd holds the reservation."""
    fd = os.open(lock_path(uuid), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = os.read(fd, 200).decode(errors="replace").strip()
        os.close(fd)
        return None, f"reserved by jobrun job {holder or '?'}"
    try:
        gpu = next((g for g in query_gpus() if g["uuid"] == uuid), None)
        procs = [p for p in query_procs() if p["uuid"] == uuid]
    except RuntimeError as e:
        os.close(fd)
        return None, str(e)
    if gpu is None:
        os.close(fd)
        return None, "not listed by nvidia-smi"
    if procs or gpu["used"] > FREE_MIB:
        os.close(fd)
        who = "; ".join(f"pid {p['pid']} {p['name']} {p['mem']} MiB" for p in procs) or "no compute process listed"
        return None, f"{who}; {gpu['used']:.0f} MiB used"
    return fd, None


def claim_gpu(fd, job):
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, 0)
    os.write(fd, job.encode())


def write_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


class LogTail:
    """Follows the job's log file (the job writes it directly, so it never depends on this process) and keeps the
    latest progress. Times are this host's clock when the line is read: containers often log in UTC."""

    def __init__(self, path):
        self.path, self.offset, self.partial = path, 0, b""
        self.progress, self.stage, self.progress_at, self.log_at = None, None, None, None
        self.first = None  # (unit, total, step, time) the rate is measured from

    def poll(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if size < self.offset:
            self.offset, self.partial = 0, b""
        if size == self.offset:
            return False
        with open(self.path, "rb") as f:
            f.seek(max(self.offset, size - TAIL_MAX))
            data = f.read(size - max(self.offset, size - TAIL_MAX))
        self.offset, now = size, time.time()
        self.log_at = now
        lines = re.split(rb"[\r\n]", self.partial + data)
        self.partial = lines.pop()
        for raw in lines:
            self.line(raw.decode(errors="replace"), now)
        return True

    def line(self, text, now):
        p = parse_progress(text)
        if not p:
            return
        self.progress_at = now
        if "stage" in p:
            self.stage = p["stage"]
            return
        key = (p["unit"], p["total"])
        if not self.first or self.first[:2] != key or p["step"] < self.first[2]:
            self.first = (*key, p["step"], now)
        if p["s_per_step"] is None and p["step"] > self.first[2] and now > self.first[3]:
            p["s_per_step"] = (now - self.first[3]) / (p["step"] - self.first[2])
        p["line"] = ANSI.sub("", text).strip()[:300]
        self.progress = p


def uses_container(cmd):
    return "$JOB_CONTAINER" in cmd or "${JOB_CONTAINER}" in cmd


def stop_container(name, grace):
    run_bounded(["docker", "stop", "--time", str(int(grace)), f"job-{name}"], grace + 15)


def signal_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except OSError:
        pass


def own_process(pid, name):
    """Is this pid still that job, and not a recycled number? On Linux its environment says so."""
    try:
        if os.getpgid(pid) != pid:
            return False
        with open(f"/proc/{pid}/environ", "rb") as f:
            return f"JOB_NAME={name}".encode() in f.read().split(b"\0")
    except FileNotFoundError:
        return sys.platform != "linux"  # no /proc here: the group check is all there is
    except OSError:
        return False


class AdoptedProcess:
    """A job this supervisor didn't start: it can watch the process but never learn its exit code."""

    returncode = None

    def __init__(self, pid, name):
        self.pid, self.name = pid, name

    def poll(self):
        return None if own_process(self.pid, self.name) else "gone"

    def wait(self):
        while self.poll() is None:
            time.sleep(0.2)


class Job:
    def __init__(self, args, job_dir):
        self.args, self.dir = args, job_dir
        self.log = os.path.join(job_dir, "log")
        self.status_path = os.path.join(job_dir, "status.json")
        self.tail = LogTail(self.log)
        self.gpu, self.gpu_fd = None, None
        self.st = {
            "name": args.name,
            "host": socket.gethostname(),
            "state": "starting",
            "start": time.time(),
            "cwd": args.cwd,
            "cmd": " ".join(args.cmd) if len(args.cmd) == 1 else shlex.join(args.cmd),
            "summary": args.summary,
            "project": args.project,
            "gpu": None,
            "pid": os.getpid(),
            "job_pid": None,
            "heartbeat": time.time(),
            "progress": None,
            "stage": None,
            "progress_at": None,
            "log_at": None,
            "exit_code": None,
            "ended": None,
            "reason": None,
            "min_runtime": args.min_runtime,
            "expect": args.expect,
        }

    def save(self, **fields):
        t = self.tail
        self.st.update(
            fields,
            heartbeat=time.time(),
            progress=t.progress,
            stage=t.stage,
            progress_at=t.progress_at,
            log_at=t.log_at,
            pid=os.getpid(),
        )
        write_json(self.status_path, self.st)

    def set_gpu(self, name, uuid, fd, forced=False):
        self.gpu, self.gpu_fd = uuid, fd
        if fd is not None:
            claim_gpu(fd, self.args.name)
        self.st["gpu"] = {"name": name, "uuid": uuid, "forced": forced}

    def take_stop_request(self):
        """Seconds of grace if a stop was asked for, consuming the request; else None."""
        path = os.path.join(self.dir, ".stop")
        try:
            with open(path) as f:
                grace = float(f.read().strip() or 30)
            os.remove(path)
            return grace
        except (OSError, ValueError):
            return None

    def stop(self, proc, grace):
        """End the job: the container and the process group get SIGTERM, what is left after the grace SIGKILL."""
        if uses_container(self.st["cmd"]):
            stop_container(self.args.name, grace)
        signal_group(proc.pid, signal.SIGTERM)
        end = time.time() + grace
        while proc.poll() is None and time.time() < end:
            time.sleep(0.2)
        signal_group(proc.pid, signal.SIGKILL)
        proc.wait()

    def wait_gpu(self, candidates):
        """False if a stop was asked for while queued."""
        while not self.gpu:
            for name, uuid in candidates:
                fd, why = try_gpu(uuid)
                if fd is not None:
                    self.set_gpu(name, uuid, fd)
                    return True
                self.save(state="queued", reason=f"GPU {name}: {why}")
            end = time.time() + GPU_POLL_S
            while time.time() < end:
                if self.take_stop_request() is not None:
                    return False
                time.sleep(min(GPU_POLL_S, 0.5))
        return True

    def child_env(self):
        env = dict(os.environ, JOB_NAME=self.args.name, JOB_DIR=self.dir, PYTHONUNBUFFERED="1")
        env["JOB_GPU"] = self.gpu or ""
        env["JOB_CONTAINER"] = f"job-{self.args.name}"
        env["CUDA_VISIBLE_DEVICES"] = self.gpu or ""  # no --gpu = no GPU
        return env

    def spawn(self):
        cmd = self.args.cmd
        if len(cmd) == 1:
            cmd = [shutil.which("bash") or "sh", "-c", cmd[0]]
        with open(self.log, "ab") as log:
            return subprocess.Popen(
                cmd,
                cwd=self.args.cwd,
                env=self.child_env(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def verdict(self, rc, runtime):
        """(state, reason) once the job has ended: exit code first, then the suspicious-success checks."""
        if rc < 0:
            return "failed", f"killed by signal {-rc} after {fmt_s(runtime)}"
        if rc != 0:
            return "failed", f"exit code {rc} after {fmt_s(runtime)}"
        if self.args.min_runtime and runtime < self.args.min_runtime:
            return "failed", f"exit 0 but ran only {fmt_s(runtime)} (--min-runtime {fmt_s(self.args.min_runtime)})"
        for f in self.args.expect:
            path = os.path.join(self.args.cwd, f)
            try:
                s = os.stat(path)
            except OSError:
                return "failed", f"exit 0 but expected file is missing: {f}"
            if s.st_size == 0:
                return "failed", f"exit 0 but expected file is empty: {f}"
            if s.st_mtime < self.st["start"] - 1:
                return "failed", f"exit 0 but expected file was not written by this run: {f}"
        return "done", f"exit 0 after {fmt_s(runtime)}"

    def verdict_when_adopted(self, _rc, runtime):
        """Without an exit code, only --expect can say a job went well."""
        if not self.args.expect:
            return (
                "failed",
                f"process ended after {fmt_s(runtime)}; it was adopted, so its exit code is unknown: read its log",
            )
        state, reason = self.verdict(0, runtime)
        return state, reason.replace("exit 0", "exit code unknown (adopted)")

    def follow(self, proc, started, verdict):
        """Track a running job until it ends, then record the outcome."""
        last = 0
        while True:
            rc = proc.poll()
            changed = self.tail.poll()
            now = time.time()
            if rc is not None:
                break
            grace = self.take_stop_request()
            if grace is not None:
                self.stop(proc, grace)
                ended = time.time()
                self.tail.poll()
                self.save(
                    state="stopped",
                    exit_code=proc.returncode,
                    ended=ended,
                    reason=f"stopped after {fmt_s(ended - started)}",
                )
                return
            if changed or now - last >= HEARTBEAT_S:
                self.save()
                last = now
            time.sleep(1 if HEARTBEAT_S >= 1 else HEARTBEAT_S)
        ended = time.time()
        state, reason = verdict(rc, ended - started)
        self.tail.poll()
        self.save(state=state, exit_code=proc.returncode, ended=ended, reason=reason)

    def adopt(self, ready):
        """Runs in the detached process, for a job whose supervisor has died."""
        try:
            self.save(state="running", reason=None)
            os.close(ready)
            proc = AdoptedProcess(self.st["job_pid"], self.args.name)
            self.follow(proc, self.st["start"], self.verdict_when_adopted)
        except Exception as e:
            self.save(reason=f"supervisor error: {e}")
            raise

    def supervise(self, candidates, ready):
        """Runs in the detached process. `ready` is written once the outcome of the launch is known."""

        def release():
            nonlocal ready
            if ready is not None:
                os.close(ready)
                ready = None

        try:
            if candidates:
                self.save(state="queued", reason="waiting for a GPU")
                release()
                if not self.wait_gpu(candidates):
                    self.save(state="stopped", ended=time.time(), reason="stopped while queued")
                    return
                self.save(reason=None)
            started = time.time()
            self.st["start"] = started
            try:
                proc = self.spawn()
            except OSError as e:
                self.save(state="failed", exit_code=127, ended=time.time(), reason=f"could not start: {e}")
                return
            self.save(state="running", job_pid=proc.pid)
            release()
            self.follow(proc, started, self.verdict)
        except Exception as e:  # the job (if any) runs on; a watcher sees the heartbeat stop
            self.save(reason=f"supervisor error: {e}")
            raise
        finally:
            release()


def fmt_s(s):
    s = int(s)
    h, m = s // 3600, s % 3600 // 60
    return f"{h}h{m:02d}" if h else f"{m}m{s % 60:02d}s" if m else f"{s}s"


def die(msg, code=2):
    print(f"jobrun: {msg}", file=sys.stderr)
    sys.exit(code)


def check_docker(args):
    """Refuse docker commands that would make the record lie; --force overrides."""
    text = " ".join(args.cmd)
    run = DOCKER_RUN.search(text)
    if not run or args.force:
        return
    if DETACHED.search(run[0]):
        die(
            "`docker run -d` returns at once, so the job would be reported finished right away; drop -d "
            "(jobrun already detaches) or pass --force"
        )
    if args.gpu:
        if "$JOB_GPU" not in text and "${JOB_GPU}" not in text:
            die(
                f'--gpu {args.gpu} but the docker command doesn\'t use $JOB_GPU: use --gpus "device=$JOB_GPU" and quote the '
                "whole command as one argument so your shell doesn't expand it, or pass --force"
            )
        if len(args.cmd) > 1:
            die("$JOB_GPU needs a shell: pass the whole command as one quoted argument")
    elif "--gpus" in run[0]:
        die(
            "the docker command takes GPUs but --gpu wasn't given, so no card would be reserved; add --gpu or pass --force"
        )
    if not uses_container(text):
        die(
            'the docker command needs --name "$JOB_CONTAINER", or a stop couldn\'t reach the container; quote the whole command as '
            "one argument so your shell doesn't expand it, or pass --force"
        )


def archive_previous(job_dir):
    """Keep the logs of an earlier run of this name; (message) if that run is still alive."""
    try:
        with open(os.path.join(job_dir, "status.json")) as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = None
    if old and old.get("state") in ("running", "starting", "queued") and pid_alive(old.get("job_pid")):
        return f"job pid {old['job_pid']} of an earlier run is still alive"
    if old or os.path.exists(os.path.join(job_dir, "log")):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime((old or {}).get("start") or time.time()))
        dest = os.path.join(job_dir, "prev", stamp)
        os.makedirs(dest, exist_ok=True)
        for f in ("status.json", "log"):
            if os.path.exists(os.path.join(job_dir, f)):
                os.replace(os.path.join(job_dir, f), os.path.join(dest, f))
    for mark in (".cleared", ".stop"):
        if os.path.exists(os.path.join(job_dir, mark)):
            os.remove(os.path.join(job_dir, mark))
    return None


def supervisor_alive(job_dir):
    fd = os.open(os.path.join(job_dir, "lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def stop_job(name, grace):
    """Ask the supervisor to stop the job and wait for it; do it here if there is no supervisor to ask. Returns an exit code."""
    job_dir, status_path = os.path.join(JOBS, name), os.path.join(JOBS, name, "status.json")

    def read():
        with open(status_path) as f:
            return json.load(f)

    try:
        st = read()
    except (OSError, ValueError):
        print(f"jobrun: no job {name}", file=sys.stderr)
        return 1
    if st.get("state") in ("done", "failed", "stopped"):
        print(f"{name}: already {st['state']}, nothing to stop")
        return 0
    with open(os.path.join(job_dir, ".stop"), "w") as f:
        f.write(str(grace))
    picked_up = time.time() + 5  # the supervisor looks every second, unless it is gone or from an older jobrun
    while os.path.exists(os.path.join(job_dir, ".stop")) and time.time() < picked_up and supervisor_alive(job_dir):
        time.sleep(0.2)
    if not os.path.exists(os.path.join(job_dir, ".stop")):
        end = time.time() + grace + 25
        while time.time() < end:
            st = read()
            if st.get("state") in ("done", "failed", "stopped"):
                print(f"{name}: {st['state']} ({st.get('reason')})")
                return 0
            time.sleep(0.2)
        print(f"jobrun: {name}: its supervisor took the request but the job hasn't ended", file=sys.stderr)
        return 1
    os.remove(os.path.join(job_dir, ".stop"))
    pid = st.get("job_pid")
    if pid and not own_process(pid, name):
        pid = None  # already gone, or that number is now someone else's: leave it alone
    if uses_container(st.get("cmd", "")):
        stop_container(name, grace)
    if pid:
        signal_group(pid, signal.SIGTERM)
        end = time.time() + grace
        while pid_alive(pid) and time.time() < end:
            time.sleep(0.2)
        signal_group(pid, signal.SIGKILL)
    if not supervisor_alive(job_dir):
        write_json(
            status_path,
            dict(st, state="stopped", ended=time.time(), reason="stopped; its supervisor was gone, exit code unknown"),
        )
    print(
        f"{name}: stopped"
        if pid or uses_container(st.get("cmd", ""))
        else f"{name}: no process of its own left to stop"
    )
    return 0


def adopt_job(name):
    """Start a new supervisor for a job that's still running without one."""
    job_dir = os.path.join(JOBS, name)
    try:
        with open(os.path.join(job_dir, "status.json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        die(f"no job {name}", 1)
    if st.get("state") in ("done", "failed", "stopped"):
        print(f"{name}: already {st['state']}, nothing to adopt")
        return
    lock = os.open(os.path.join(job_dir, "lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        die(f"job {name} still has its supervisor: nothing to adopt", 1)
    if not st.get("job_pid") or not own_process(st["job_pid"], name):
        die(f"job {name} has no process left to follow", 1)
    args = argparse.Namespace(
        name=name,
        cwd=st["cwd"],
        cmd=[st["cmd"]],
        summary=st.get("summary", ""),
        project=st.get("project"),
        min_runtime=st.get("min_runtime") or 0,
        expect=st.get("expect") or [],
    )
    job = Job(args, job_dir)
    job.st = st
    gpu = st.get("gpu")
    if gpu:  # take the reservation back if nobody else has
        fd = os.open(lock_path(gpu["uuid"]), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            job.gpu, job.gpu_fd = gpu["uuid"], fd
            claim_gpu(fd, name)
        except OSError:
            os.close(fd)
    daemonize(job, lock, job.adopt)


def parse_start(argv):
    if "--" not in argv:
        die("missing --: jobrun start <name> [options] -- <cmd>; --help has the details")
    cut = argv.index("--")
    p = argparse.ArgumentParser(prog="jobrun start", add_help=False)
    p.add_argument("name")
    p.add_argument("--gpu")
    p.add_argument("--wait-gpu", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--summary", default="")
    p.add_argument("--project")
    p.add_argument("--min-runtime", type=float, default=0)
    p.add_argument("--expect", action="append", default=[])
    args = p.parse_args(argv[:cut])
    args.cmd = argv[cut + 1 :]
    args.cwd = os.path.abspath(os.path.expanduser(args.cwd))
    if not NAME_OK.match(args.name):
        die(f"bad job name {args.name!r}: letters, digits, . _ - only")
    if not args.cmd or not args.cmd[0].strip():
        die("no command after --")
    if not os.path.isdir(args.cwd):
        die(f"--cwd {args.cwd} is not a directory")
    if args.wait_gpu and not args.gpu:
        die("--wait-gpu without --gpu")
    check_docker(args)
    return args


def reserve_gpu(job):
    """Take the card now if it's free. Returns the cards to queue for, if the job has to wait for one."""
    args = job.args
    if not args.gpu:
        return None
    try:
        candidates = resolve_gpu(args.gpu)
    except RuntimeError as e:
        die(str(e), EXIT_GPU)
    whys = []
    for name, uuid in candidates:
        fd, why = try_gpu(uuid)
        if fd is not None:
            job.set_gpu(name, uuid, fd)
            return None
        whys.append(f"GPU {name} ({uuid}) is busy: {why}")
    if args.force:
        job.set_gpu(*candidates[0], None, forced=True)
        return None
    if args.wait_gpu:
        return candidates
    die("\n".join(whys) + "\nRefusing to start: --wait-gpu queues for it, --force overrides.", EXIT_GPU)


def daemonize(job, lock, work):
    """Fork the supervisor, which runs work(ready). The parent reports how the launch went and exits; the child never returns."""
    r, w = os.pipe()
    sys.stdout.flush()
    if os.fork():
        os.close(w)
        os.close(lock)
        if job.gpu_fd is not None:
            os.close(job.gpu_fd)
        os.read(r, 1)  # EOF when the supervisor has started the job or queued it
        with open(job.status_path) as f:
            st = json.load(f)
        gpu = f" on GPU {st['gpu']['name']} ({st['gpu']['uuid']})" if st.get("gpu") else ""
        reason = f" ({st['reason']})" if st.get("reason") else ""
        log = job.log.replace(os.path.expanduser("~"), "~", 1)
        print(f"{job.args.name}: {st['state']}{gpu}, supervisor pid {st['pid']}, log {log}{reason}")
        sys.exit(1 if st["state"] == "failed" else 0)
    os.close(r)
    os.setsid()
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN)  # the job's end decides; on a reboot the watcher sees the heartbeat stop
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):  # nothing of ssh's stays open, or ssh would wait for the job
        os.dup2(null, fd)
    work(w)
    os._exit(0)


def start(argv):
    args = parse_start(argv)
    job_dir = os.path.join(JOBS, args.name)
    os.makedirs(job_dir, exist_ok=True)
    # Held by the supervisor for its whole life; the kernel drops it if the supervisor dies.
    lock = os.open(os.path.join(job_dir, "lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        die(f"job {args.name} is already running ({job_dir})", EXIT_NAME)
    busy = archive_previous(job_dir)
    if busy:
        die(f"job {args.name}: {busy}; pick another name", EXIT_NAME)
    job = Job(args, job_dir)
    queue = reserve_gpu(job)
    job.save()
    daemonize(job, lock, lambda ready: job.supervise(queue, ready))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else ""
    if command in ("-h", "--help"):
        print(__doc__)
    elif command == "start":
        start(argv[1:])
    elif command == "stop":
        p = argparse.ArgumentParser(prog="jobrun stop", add_help=False)
        p.add_argument("name")
        p.add_argument("--grace", type=float, default=30)
        args = p.parse_args(argv[1:])
        if not NAME_OK.match(args.name):
            die(f"bad job name {args.name!r}")
        sys.exit(stop_job(args.name, args.grace))
    elif command == "adopt":
        if len(argv) != 2 or not NAME_OK.match(argv[1]):
            die("usage: jobrun adopt <name>")
        adopt_job(argv[1])
    elif command == "discover-gpus":
        try:
            print(json.dumps(discover()))
        except RuntimeError as e:
            print("{}")
            print(f"jobrun: {e}", file=sys.stderr)
    else:
        die("usage: jobrun start|stop|adopt|discover-gpus ...; --help has the details")


if __name__ == "__main__":
    main()
