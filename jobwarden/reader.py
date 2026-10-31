"""What jobs exist and how they are doing, read from the hosts (stdlib only, no display).

~/jobs/<name>/status.json on the host, written by jobrun, is the only record. Nothing is saved on this machine: each process
reads the hosts over their one shared SSH connection, and only remembers its own last read, for when a host stops answering.

A job's state is one of: running, queued, orphan (it outlived its supervisor), unreachable, unreadable, done, failed, stopped.
"""

import json
import os
import re
import threading
import time

from .core import LOCAL, Transport, UnknownHost, Unreachable, get_host, has_hosts_file, load_hosts

STALE_S = float(os.environ.get("JOBWARDEN_STALE", 60))  # heartbeat older than this: the supervisor is gone
STALL_MIN, UNREACH_MIN = 20, 10
CONTROL = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]")
ORDER = {
    "running": 0,
    "queued": 0,
    "orphan": 0,
    "unreachable": 0,
    "unreadable": 0,
    "failed": 1,
    "stopped": 2,
    "done": 2,
}
ENDED = ("done", "failed", "stopped")
with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "on_host.py")) as _f:
    ON_HOST = _f.read()


def keep_s():
    return float(os.environ.get("JOBWARDEN_KEEP_MIN", 60)) * 60


def clean(text):
    """Logs and status come from the host: nothing in them may drive a terminal."""
    return CONTROL.sub("", str(text))


def fmt_dur(s):
    s = int(max(s, 0))
    return f"{s // 3600}h{s % 3600 // 60:02d}" if s >= 3600 else f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def all_hosts():
    """{label: settings}: the hosts file, and this machine unless it is in there already."""
    hosts = load_hosts()
    if not any(h.get("ssh") == "local" for h in hosts.values()):
        hosts = dict(hosts, **{LOCAL: {"ssh": "local"}})
    return hosts


def fetch(transport, names=None):
    """The host's answer: {"now": its clock, "jobs": {name: record}}; raises Unreachable."""
    rc, out, err = transport.run("python3 -", stdin_text=f"{ON_HOST}\nmain({names!r})\n")
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise Unreachable(err.strip().splitlines()[-1] if err.strip() else f"reading the host: exit {rc}") from None


def blank_job(name, host):
    """Every field a job has, so readers never have to wonder: a job not seen yet stays like this."""
    return dict(
        name=name,
        host=host,
        project=None, dir="",
        state="unreadable",
        note="",
        status={},
        log=[],
        summary="",
        gpu=None,
        step=0,
        total=0,
        frac=0.0,
        stage="",
        metrics={},
        eta_s=None,
        updated="",
        last_at=None,
        start_at=None,
        ended_at=None,
        unreachable_since=None,
        temporary=False,
        cleared=False,
    )


def assess(st, now, job_alive):
    """(state, note) from a status."""
    state = st.get("state")
    if state in ENDED:
        return state, st.get("reason") or ""
    if now - st.get("heartbeat", 0) <= STALE_S:
        return ("queued", st.get("reason") or "") if state == "queued" else ("running", "")
    since = fmt_dur(now - st.get("heartbeat", now))
    if job_alive:
        return "orphan", f"supervisor gone for {since}, job still alive (its exit code won't be recorded)"
    return (
        "failed",
        f"supervisor gone for {since} and no job process left (host rebooted or killed?), exit code unknown",
    )


def describe(name, record, now, label, settings):
    """A job as a plain dict, None if the host has no such record. Times are on this machine's clock, computed from the host's,
    so a skewed pod can't fake a stall or hide one."""
    if not record:
        return None
    st = record["status"]
    if st is None:
        return dict(blank_job(name, label), note="unreadable status.json, retrying")
    wall = time.time()
    state, note = assess(st, now, record["job_alive"])
    log = [clean(x) for x in record["log"]]
    p = st.get("progress") or {}
    step, total = p.get("step", 0), p.get("total", 0)
    last = st.get("progress_at") or st.get("log_at") or st.get("start") or now
    over = state in ENDED
    note = clean(note)
    if over:
        if state == "failed" and log:
            note += f" · last: {log[-1][:120]}"
        if settings.get("temporary"):
            note += f" · {label} is temporary and may still be billing: stop it?"
    last_at = wall - max(now - last, 0)
    return dict(
        blank_job(name, label),
        project=st.get("project"),
        state=state,
        note=note,
        status=st,
        log=log,
        summary=clean(st.get("summary") or ""),
        gpu=clean((st.get("gpu") or {}).get("name") or "") or None,
        step=step,
        total=total,
        frac=1.0 if state == "done" else min(step / total, 1.0) if total else 0.0,
        stage=clean(st.get("stage") or p.get("unit") or ""),
        metrics={clean(k): clean(v) for k, v in (p.get("metrics") or {}).items()},
        eta_s=max(total - step, 0) * p["s_per_step"] if p.get("s_per_step") and total else None,
        updated=time.strftime("%H:%M:%S", time.localtime(last_at)),
        last_at=last_at,
        start_at=wall - max(now - st.get("start", now), 0),
        ended_at=wall - max(now - (st.get("ended") or st.get("heartbeat") or now), 0) if over else None,
        temporary=bool(settings.get("temporary")),
        cleared=record["cleared"],
        dir=record["dir"],
    )


def no_hosts_note():
    """Why a remote job can't be found from here, if that's the reason."""
    if has_hosts_file():
        return ""
    return f"; no .jobwarden/hosts.json from {os.getcwd()} upwards, so only this machine is known"


def no_job(name, down=()):
    """What to say about a job that isn't found."""
    why = f"not answering: {', '.join(down)}; " if down else ""
    return f"no job {name} ({why}`jobwarden status --all` lists the jobs of this project's hosts{no_hosts_note()})"


def is_stalled(job, minutes=STALL_MIN, awake_since=0):
    """No progress line for this long. awake_since: don't count before then (a local job doesn't run while this machine sleeps)."""
    return (
        job.get("state") in ("running", "orphan")
        and time.time() - max(job["last_at"] or time.time(), awake_since) > minutes * 60
    )


def visible(job, now=None):
    """Views don't show a job cleared by hand, or $JOBWARDEN_KEEP_MIN minutes after it ended or its host stopped answering."""
    now = now or time.time()
    since = job["ended_at"] or job["unreachable_since"]
    return not job["cleared"] and not (since and now - since > keep_s())


def pick(jobs, project=None, include_hidden=False):
    """The jobs a view shows, sorted."""
    out = [j for j in jobs if j and (not project or j["project"] == project) and (include_hidden or visible(j))]
    return sorted(out, key=lambda j: (ORDER.get(j["state"], 9), j["name"]))


def public(job):
    """A job for JSON output: the documented fields, without its raw status.json."""
    return {k: v for k, v in job.items() if k != "status"}


class SleepWatch:
    """Notices this machine having slept: monotonic time doesn't count sleep, the wall clock does."""

    def __init__(self):
        self.at = (time.time(), time.monotonic())

    def slept(self):
        """Seconds asleep since the last call, 0 if it didn't sleep."""
        now = (time.time(), time.monotonic())
        gap = (now[0] - self.at[0]) - (now[1] - self.at[1])
        self.at = now
        return gap if gap > 30 else 0


class Reader:
    """One process's view of the hosts. It keeps only its last read of each, to show when a host stops answering."""

    def __init__(self):
        self.transports, self.last, self.down_since, self.why = {}, {}, {}, {}

    def down(self):
        """{host: why} for the hosts that didn't answer their last read."""
        return {label: self.why[label] for label in list(self.down_since)}

    def transport(self, label, settings):
        if label not in self.transports:
            self.transports[label] = Transport(label, settings)
        return self.transports[label]

    def cut_off(self, label, job):
        """A job as last seen on a host that doesn't answer. One that had ended stays as it was: that can't change."""
        if job["state"] in ENDED:
            return job
        return dict(
            job,
            state="unreachable",
            note=f"cannot reach {label}: {clean(self.why[label])}",
            unreachable_since=self.down_since[label],
        )

    def read(self, label, names=None):
        """{name: job, or None if the host has no record} for these jobs, or all of the host's. A host that doesn't answer
        gives its last known jobs, marked so."""
        settings = get_host(label)
        saved = self.last.setdefault(label, {})
        try:
            answer = fetch(self.transport(label, settings), names)
        except Unreachable as e:
            self.down_since.setdefault(label, time.time())
            self.why[label] = str(e)
            return {n: self.cut_off(label, saved.get(n) or blank_job(n, label)) for n in (names or saved)}
        self.down_since.pop(label, None)
        out = {n: describe(n, answer["jobs"].get(n), answer["now"], label, settings) for n in (names or answer["jobs"])}
        if names is None:
            saved.clear()
        saved.update({n: j for n, j in out.items() if j})
        return out

    def each_host(self, fn, hosts):
        """fn(label) for each host at once, so a slow host adds no time to the others' reads."""
        threads = [threading.Thread(target=fn, args=(label,), daemon=True) for label in hosts]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def jobs(self, project=None, include_hidden=False, host=None):
        """The jobs of all hosts, or of one, sorted, read now. Returns once each host has answered or timed out."""
        hosts = all_hosts()
        labels = [host] if host in hosts else [] if host else list(hosts)
        found = {}
        self.each_host(lambda label: found.update({label: self.read(label)}), labels)
        return pick([j for label in labels for j in found.get(label, {}).values()], project, include_hidden)

    def locate(self, name):
        """([hosts that have this job], [hosts that don't answer]), asking them all at once."""
        answers = {}
        self.each_host(lambda label: answers.update({label: self.read(label, [name])[name]}), all_hosts())
        return [h for h, j in answers.items() if j and h not in self.down_since], [
            h for h in answers if h in self.down_since
        ]

    def host_of(self, name):
        """The one host that has this job; raises LookupError saying why there isn't one."""
        found, down = self.locate(name)
        if len(found) > 1:
            raise LookupError(f"job {name} is on several hosts ({', '.join(found)}): use --host")
        if not found:
            raise LookupError(no_job(name, down))
        return found[0]


class Live:
    """jobs() that never waits: one thread per host reads it every `interval` seconds, so a dead host holds back nothing, and
    jobs() and down() answer at once from the last reads. Empty until the first answers. on_read(live, label, jobs), if given, runs in
    the host's thread after each of its reads."""

    def __init__(self, project=None, interval=10, host=None, include_hidden=False, on_read=None):
        self.project, self.interval, self.host, self.include_hidden, self.on_read = (
            project,
            interval,
            host,
            include_hidden,
            on_read,
        )
        self.reader, self.found, self.threads = Reader(), {}, {}
        threading.Thread(target=self.spawn_forever, daemon=True).start()

    def labels(self):
        try:
            return [self.host] if self.host else list(all_hosts())
        except (OSError, ValueError):  # the hosts file is being edited
            return []

    def spawn_forever(self):
        while True:
            for label in self.labels():
                if label not in self.threads:
                    self.threads[label] = threading.Thread(target=self.read_forever, args=(label,), daemon=True)
                    self.threads[label].start()
            time.sleep(self.interval)

    def read_forever(self, label):
        try:
            while label in self.labels():
                found = self.reader.read(label)
                self.found[label] = pick(found.values(), self.project, self.include_hidden)
                if self.on_read:
                    self.on_read(self, label, self.found[label])
                time.sleep(self.interval)
        except (UnknownHost, OSError, ValueError):
            pass
        finally:  # the host left the file, or its file was unreadable: spawn_forever starts it again if it comes back
            self.found.pop(label, None)
            self.threads.pop(label, None)

    def jobs(self):
        return pick([j for jobs in list(self.found.values()) for j in jobs], self.project, self.include_hidden)

    def down(self):
        """{host: why} for the hosts that didn't answer their last read."""
        return self.reader.down()
