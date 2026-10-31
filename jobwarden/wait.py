"""Block until a job needs attention, then print one line and exit (stdlib only; only --check runs anything on the host).

Meant to be started by the Claude session that launched the job, as a background command: Claude Code wakes the session
up when it exits. Any script can use it too. It reads the job on its host; nothing else needs to be running.

Exits when the job is done or failed, its supervisor has died, its host has been unreachable for --unreachable minutes, its last progress line is
older than --stall minutes, its record is gone, or your --check says so. jobrun decides done or failed when the job ends: a
non-zero exit, under --min-runtime, or a missing, empty or old --expect file is failed; anything else is done. A job ended with
jobwarden stop is stopped. The printed line starts with "JOB <name> <EVENT>:". --notify runs a command of yours on each event (see jobwarden/notify.py).

--check wakes you before something goes wrong, for what only your project can tell: a loss going to NaN, a disk filling up.
Every --check-every minutes while the job runs, and once when it exits normally, the command runs on the job's host, in its
--cwd, with $JOB_NAME and
$JOB_DIR (its log and status.json) set, and has 30 s. Exit 0: all is well. Exit 1: wake up, and what it printed is the message.
Anything else, or three runs in a row out of time, means the check itself is broken, which wakes you too rather than leaving you
unprotected. The job is never touched: to keep waiting, start jobwarden wait again.
    jobwarden wait ft1 --check '! grep -q "loss nan" "$JOB_DIR/log" || { echo "loss is NaN"; exit 1; }'
    jobwarden wait ft1 --check 'python3 tools/check_training.py' --check-every 10

Exit code: 0 done, 1 failed, 2 bad usage or unknown host, 3 unreachable, 4 same name on several hosts, 5 cleared / never
appeared, 6 the check woke you, 7 stopped, 8 stalled, 9 orphan, 70 jobwarden itself crashed.
"""

import shlex
import sys
import time

from .core import Unreachable, get_host, is_local
from .notify import Notifier, title
from .reader import STALE_S, STALL_MIN, UNREACH_MIN, Reader, SleepWatch, blank_job, clean, is_stalled, no_hosts_note

NOTIFY_EVENT = {"CHECK BROKEN": "check", "CLEARED": "gone", "NEVER APPEARED": "gone"}


def alarm(job, seen, started, args, down, awake_since=0):
    """(exit code, event, detail) when the job needs attention, else None."""
    now = time.time()
    if job is None or job["cleared"]:
        if seen:
            return 5, "CLEARED", "its record is gone or was cleared"
        if down and now - started > args.unreachable * 60:
            return 3, "UNREACHABLE", f"no such job found, and not answering: {', '.join(down)}"
        if now - started > args.appear * 60:
            why = f" (not answering: {', '.join(down)})" if down else ""
            return 5, "NEVER APPEARED", f"no such job after {args.appear:g} min{why}"
        return None
    state, note = job["state"], job["note"]
    where = f"step {job['step']}/{job['total']}" if job["total"] else ""
    if state == "done":
        return 0, "DONE", note or "finished"
    if state == "failed":
        return 1, "FAILED", f"{note or 'see logs'} ({where})".replace(" ()", "")
    if state == "stopped":
        return 7, "STOPPED", note or "stopped"
    if state == "unreachable":
        since = job["unreachable_since"] or now
        if now - since > args.unreachable * 60:
            return 3, "UNREACHABLE", f"for {(now - since) / 60:.0f} min: {note}"
        return None
    if state == "orphan" and now - awake_since > STALE_S:
        return (
            9,
            "ORPHAN",
            f"its supervisor died, but the job is still running: `jobwarden adopt {job['name']}` tracks it again",
        )
    if is_stalled(job, args.stall, awake_since):
        age = (now - job["last_at"]) / 60
        if not job["status"].get("progress") and not job["status"].get("stage"):
            return (
                8,
                "STALLED",
                (
                    f"no output since {job['updated']} ({age:.0f} min), and no progress line ever seen: it may be "
                    'healthy but silent. Progress lines look like "step 1/9"; a silent job needs a larger --stall'
                ),
            )
        return 8, "STALLED", f"no progress since {job['updated']} ({age:.0f} min), {where}, stage {job['stage'] or '?'}"
    return None


def run_check(reader, label, job, command):
    """(exit code, event, detail) if the check wakes us, None if all is well; raises Unreachable if it got no answer in time."""
    q = shlex.quote
    script = (
        f'cd {q(job["status"].get("cwd") or ".")} || exit 125; JOB_NAME={q(job["name"])} JOB_DIR="$HOME/.jobwarden/jobs/"{q(job["name"])} '
        f"sh -c {q(command)}"
    )
    rc, out, err = reader.transport(label, get_host(label)).run(script)
    said = " | ".join(clean(x).strip() for x in (out + err).strip().splitlines())[-500:]
    if rc == 0:
        return None
    if rc == 1:
        return 6, "CHECK", said or "the check says something is wrong"
    return 6, "CHECK BROKEN", f"exit {rc}: {said}"


def add_arguments(p, job_name):
    p.add_argument("name", type=job_name)
    p.add_argument("--host", help="only if the name exists on several hosts (default: look on all)")
    p.add_argument(
        "--stall", type=float, default=STALL_MIN, help=f"minutes without a progress line (default {STALL_MIN})"
    )
    p.add_argument(
        "--unreachable", type=float, default=UNREACH_MIN, help=f"minutes of unreachable host (default {UNREACH_MIN})"
    )
    p.add_argument("--appear", type=float, default=10, help="minutes to wait for the job to exist (default 10)")
    p.add_argument("--notify", metavar="COMMAND", help="shell command to run on each event (see jobwarden/notify.py)")
    p.add_argument("--check", metavar="COMMAND", help="your own health check, run on the host (see above)")
    p.add_argument("--check-every", type=float, default=5, metavar="MIN", help="minutes between checks (default 5)")
    p.add_argument("--interval", type=float, default=15, help="seconds between reads of the job (default 15)")


def run(args):
    started, seen, label, down = time.time(), False, args.host, []
    reader = Reader()
    clock = SleepWatch()
    warned, unreadable_since = False, None
    next_check, no_answer, awake_since = time.time() + args.check_every * 60, 0, 0
    while True:
        if not label:
            found, down = reader.locate(args.name)
            if len(found) > 1:
                print(f"JOB {args.name} is on several hosts ({', '.join(found)}): use --host")
                return 4
            label = found[0] if found else None
            if not label and not warned:
                warned = True
                print(f"no job {args.name} yet{no_hosts_note()}", file=sys.stderr)
        if clock.slept() and label and is_local(get_host(label)):
            awake_since = time.time()  # a local job was asleep too: that's not a stall
        job = reader.read(label, [args.name])[args.name] if label else None
        seen = seen or (job is not None and job["state"] != "unreachable")
        hit = alarm(job, seen, started, args, down, awake_since)
        if not hit and args.check and job and job["state"] == "running" and time.time() >= next_check:
            next_check = time.time() + args.check_every * 60
            try:
                hit, no_answer = run_check(reader, label, job, args.check), 0
            except Unreachable as e:  # the host answered the read just before, so the likely culprit is the check
                no_answer += 1
                if no_answer >= 3:
                    hit = 6, "CHECK BROKEN", f"{no_answer} runs in a row without an answer: {clean(e)}"
        if job and job["state"] == "unreadable":
            unreadable_since = unreadable_since or time.time()
            if not hit and time.time() - unreadable_since > args.stall * 60:
                hit = 8, "STALLED", f"its status.json has been unreadable for {args.stall:g} min"
        else:
            unreadable_since = None
        if hit and hit[1] == "DONE" and args.check:  # a short job may never have reached a check
            try:
                last = run_check(reader, label, job, args.check)
                hit = (last[0], last[1], f"{last[2]} (the job itself exited normally)") if last else hit
            except Unreachable:
                pass
        if hit:
            code, event, detail = hit
            print(f"JOB {args.name} {event}: {detail}", flush=True)
            if args.notify:
                word = NOTIFY_EVENT.get(event, event.lower())
                Notifier(args.notify).send(word, title(args.name, word), detail, job or blank_job(args.name, label))
            return code
        time.sleep(min(args.interval, max(next_check - time.time(), 0.1)) if args.check else args.interval)
