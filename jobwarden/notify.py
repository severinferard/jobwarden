"""Notifications, per reader process (stdlib only). jobwarden knows no terminal: the user gives a command, and it runs it.

jobwarden wait and jobwarden watch take --notify '<command>'. jobwarden wait runs it once, for the event it exits on. jobwarden watch runs it
for each change between a job it just read and what it saw last, in memory. The command runs in a shell with these set:
    JOBWARDEN_TITLE  JOBWARDEN_BODY  JOBWARDEN_JOB  JOBWARDEN_HOST
    JOBWARDEN_EVENT  done, failed, stopped, stalled, progressing, unreachable, reachable, check, gone
Every attempt goes in ~/.cache/jobwarden/notify.log; a failed one is not retried.
"""

import os
import sys
import time

from .core import run_bounded
from .reader import ENDED, STALL_MIN, UNREACH_MIN, is_stalled

COMMAND_TIMEOUT_S = 10


def events(prev, now, job):
    """[(event, title, body)] between what this process saw last (None at first) and now, for a job just read."""
    if prev is None:
        return []
    name, upd = job["name"], job["updated"]
    if now["state"] in ENDED:
        return [(now["state"], title(name, now["state"]), job["note"])] if prev["state"] != now["state"] else []
    out = []
    if now["stalled"] and not prev["stalled"]:
        out.append(("stalled", f"{name} stalled", f"no progress since {upd}"))
    elif prev["stalled"] and not now["stalled"] and now["state"] != "unreachable":
        out.append(("progressing", f"{name} progressing again", f"last progress {upd}"))
    if now["cut_off"] and not prev["cut_off"]:
        out.append(("unreachable", f"{name}: host unreachable", f"cannot reach {job['host']}"))
    elif prev["cut_off"] and not now["cut_off"]:
        out.append(("reachable", f"{name} reachable again", job["stage"] or "running"))
    return out


def title(name, event):
    return f"{name} {'finished' if event == 'done' else event}"


def snapshot(job):
    since = job["unreachable_since"]
    cut_off = job["state"] == "unreachable" and since is not None and time.time() - since > UNREACH_MIN * 60
    return dict(state=job["state"], stalled=is_stalled(job, STALL_MIN), cut_off=cut_off)


class Notifier:
    def __init__(self, command):
        self.command = command
        self.seen = {}

    def check(self, job):
        if not self.command or not job:
            return
        now = snapshot(job)
        for event, heading, body in events(self.seen.get(job["name"]), now, job):
            self.send(event, heading, body, job)
        self.seen[job["name"]] = now

    def send(self, event, heading, body, job):
        env = dict(
            os.environ,
            JOBWARDEN_EVENT=event,
            JOBWARDEN_TITLE=heading,
            JOBWARDEN_BODY=body,
            JOBWARDEN_JOB=job["name"],
            JOBWARDEN_HOST=job["host"] or "",
        )
        try:
            rc, _, err = run_bounded(["sh", "-c", self.command], COMMAND_TIMEOUT_S, env=env)
            outcome = (
                "sent" if rc == 0 else f"failed ({'timeout' if rc is None else f'exit {rc}'}): {err.strip()[:200]}"
            )
        except OSError as e:
            outcome = f"failed: {e}"
        if outcome != "sent":
            print(f"jobwarden: --notify {outcome}", file=sys.stderr)
        try:
            path = os.path.expanduser("~/.cache/jobwarden/notify.log")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                f.write(f"{time.strftime('%F %T')} pid {os.getpid()} {outcome}: {heading}\n")
        except OSError:
            pass
