"""Follow jobs live (stdlib only, read-only on the host). It is only a viewer: closing it interrupts nothing.

    jobwarden watch ft1 [ft2...]        those jobs in detail; polling stops when they have ended, the last frame stays on screen
    jobwarden watch [--project NAME]    every job, in one compact view that also picks up new jobs; it never ends: use it in a Dock
    --once                           print one frame and exit
    --json                           for programs: one line of JSON per host at each read (see below), with or without names;
                                     for a single reading, jobwarden status --json

Named jobs are found on the hosts; --host is only for a name that exists on several, or to look at one host. In detail it
shows the summary on top, bar, step/total, stage, ETA, metrics, the job's reserved card, last progress vs last poll.

--json lines look like {"version": 1, "host": "box1", "reachable": true, "error": null, "jobs": [...]}, and "jobs" has the
fields of jobwarden.jobs() without the raw status. With names it ends once they have all ended.

--notify runs a command of yours on each change it sees while open (see jobwarden/notify.py).
A sleep of this machine is reported: a job on a remote host carries on, a local one pauses, and nothing is watched meanwhile.

--config points to a small Python file in the project, for what is specific to the job (all optional):
    SUMMARY = "2-4 lines: what it is for, inputs, outputs and where they go"   (default: jobrun --summary, else the command)
    METRICS = ["loss", "wer"]                 metrics to show (default: all of the progress line)
    def results(job): return ["lines", ...]   extra lines under the job's block
`job` is a job as jobwarden.jobs() gives it. Hooks get 20 s, then are skipped
for that refresh: a project's hook can't freeze the watcher. Live GPU numbers are not jobwarden's business; for those, have
results() call hoststats with job["status"]["gpu"]["uuid"] (see examples/job_config.py).

Exit code: 0 ok, 1 no such job, 3 host unreachable (--once), 2 bad usage.
"""

import importlib.util
import json
import os
import re
import shutil
import signal
import sys
import threading
import time

from .core import get_host
from .notify import Notifier
from .reader import ENDED, Live, Reader, SleepWatch, clean, fmt_dur, public

HOOK_S = 20
BAR = 30
ICONS = {
    "running": "●",
    "queued": "◌",
    "done": "✓",
    "failed": "✗",
    "stopped": "○",
    "orphan": "⚠",
    "unreachable": "⚠",
    "unreadable": "?",
}
RESET, DIM, BOLD, RED, GREEN, YELLOW = "\033[0m", "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m"
COLORS = {"done": GREEN, "failed": RED, "orphan": YELLOW, "unreachable": YELLOW}


def plain(text):
    """Colours only where they can be seen: not in a pipe, nor with NO_COLOR set."""
    if not sys.stdout.isatty():
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    return re.sub(r"\x1b\[[0-9;]*m", "", text) if os.environ.get("NO_COLOR") else text


def closed(*_):
    raise KeyboardInterrupt


def quit_on_close():
    """Closing the tab ends the viewer the same way Ctrl-C does."""
    signal.signal(signal.SIGHUP, closed)
    signal.signal(signal.SIGTERM, closed)


def bar(step, total):
    k = round(min(step / total, 1) * BAR) if total else 0
    return f"[{'█' * k}{'░' * (BAR - k)}]"


def gpu_text(st):
    g = st.get("gpu")
    if not g:
        return None
    return f"GPU {g['name']} ({g['uuid']})" + (" (forced)" if g.get("forced") else "")


class Config:
    def __init__(self, path):
        self.mod = None
        if path:
            spec = importlib.util.spec_from_file_location("jobwarden_job_config", os.path.expanduser(path))
            self.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.mod)

    def get(self, attr, default=None):
        return getattr(self.mod, attr, default) if self.mod else default

    def call(self, attr, job, default=None):
        """A project's hook, bounded in time and caught: it must not take the watcher down."""
        fn = self.get(attr)
        if not fn:
            return default
        box = {}

        def work():
            try:
                box["v"] = fn(job)
            except Exception as e:
                box["e"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(HOOK_S)
        if t.is_alive():
            box["e"] = f"took more than {HOOK_S} s"
        if "e" in box:
            job.setdefault("hook_errors", []).append(f"{attr}: {box['e']}")
            return default
        return box.get("v", default)


class Watch:
    def __init__(self, args):
        self.args = args
        self.reader = Reader()
        self.clock, self.slept = SleepWatch(), ""
        self.config = Config(args.config)
        self.locate()

    def locate(self):
        """Which host has each job. Raises LookupError if one can't be found."""
        self.labels = {}
        for name in self.args.names:
            if self.args.host:
                self.labels[name] = self.args.host
                continue
            self.labels[name] = self.reader.host_of(name)
        self.hosts = {label: get_host(label) for label in self.labels.values()}

    def read(self):
        """{name: job or None} from their hosts."""
        jobs = {}
        for label in self.hosts:
            jobs.update(self.reader.read(label, [n for n in self.args.names if self.labels[n] == label]))
        return jobs

    def draw(self, name, d):
        """The lines of one job in detail."""
        if not d:
            return [f"{BOLD}{name}{RESET}  {RED}its record is gone from the host{RESET}"]
        if d["state"] == "unreadable" or not d["status"]:
            return [f"{BOLD}{name}{RESET}  {YELLOW}{clean(d.get('note') or 'no data yet')}{RESET}"]
        state, note, log, step, total, st, eta = (
            d["state"],
            d["note"],
            d["log"],
            d["step"],
            d["total"],
            d["status"],
            d["eta_s"],
        )
        icon = f"{COLORS.get(state, '')}{ICONS[state]}{RESET}"
        lines = [f"{icon} {BOLD}{name}{RESET} · {state}" + (f" · {d['stage']}" if d["stage"] else "")]
        if total:
            lines.append(
                f"  {bar(step, total)} {step}/{total}"
                + (f" · ETA {fmt_dur(eta)}" if eta is not None and state in ("running", "orphan") else "")
            )
        wanted, values = self.config.get("METRICS"), d["metrics"]
        metrics = {k: values[k] for k in wanted if k in values} if wanted else values
        if metrics:
            lines.append("  " + " · ".join(f"{k} {v}" for k, v in metrics.items()))
        g = gpu_text(st)
        if g:
            lines.append("  " + clean(g))
        if note:
            lines.append(
                f"  {RED if state == 'failed' else YELLOW if state in ('orphan', 'unreachable') or d['temporary'] else ''}{note}{RESET}"
            )
        lines.append(
            f"  {DIM}started {time.strftime('%H:%M:%S', time.localtime(d['start_at']))}"
            f" · last progress {time.strftime('%H:%M:%S', time.localtime(d['last_at']))} ({fmt_dur(time.time() - d['last_at'])} ago){RESET}"
        )
        lines += ["  " + clean(x) for x in self.config.call("results", d, []) or []]
        lines += [f"  {YELLOW}hook {clean(e)}{RESET}" for e in d.get("hook_errors", [])]
        if state == "running" and not (st.get("progress") or st.get("stage")):
            lines.append(
                f"  {DIM}No progress line seen yet, so there's no bar. Print lines like: step 1/9 loss 0.8{RESET}"
            )
        if state == "failed" or not (st.get("progress") or st.get("stage")):
            lines += [f"  {DIM}log: {x}{RESET}" for x in log[-(len(log) if state == "failed" else 3) :]]
        return lines

    def frame(self, jobs):
        """The whole screen, and whether every job has ended."""
        text = self.config.get("SUMMARY")
        if text:
            head = [text.rstrip()]
        else:
            head = [
                clean(d["status"].get("summary") or d["status"].get("cmd") or d["name"])[:300]
                for d in jobs.values()
                if d and d["status"]
            ]
        where = ", ".join(f"{label}{' (temporary)' if h.get('temporary') else ''}" for label, h in self.hosts.items())
        blocks, running = [], []
        for name in self.args.names:
            d = jobs.get(name)
            blocks += self.draw(name, d) + [""]
            if d and d["state"] not in ENDED:
                running.append(name)
        foot = ("Running now: " + ", ".join(running)) if running else "Running now: nothing, all jobs have ended"
        return "\n".join(
            head + [f"{DIM}host {where}{RESET}", ""] + blocks + [f"{foot} · polled {time.strftime('%H:%M:%S')}"]
        ), not running

    def sleep_note(self):
        """If this machine has slept, say so under the frame."""
        gap = self.clock.slept()
        if gap:
            until = time.strftime("%H:%M:%S")
            self.slept = f"{YELLOW}Asleep until {until} for {fmt_dur(gap)}: nothing was watched meanwhile{RESET}"
        return [self.slept] if self.slept else []

    def once(self):
        jobs = self.read()
        print(plain(self.frame(jobs)[0]))
        missing = [n for n, d in jobs.items() if d is None]
        if missing:
            print(f"no job {missing[0]} on {self.labels[missing[0]]}", file=sys.stderr)
            return 1
        return 3 if any(d["state"] == "unreachable" for d in jobs.values()) else 0

    def loop(self):
        quit_on_close()
        try:
            notifier = Notifier(self.args.notify)
            while True:
                jobs = self.read()
                for job in jobs.values():
                    notifier.check(job)
                text, finished = self.frame(jobs)
                text = "\n".join([text] + self.sleep_note())
                footer = f"\n\nrefresh every {self.args.interval:g} s · Ctrl-C to quit" if not finished else ""
                print(plain("\033[2J\033[H" + text + footer), flush=True)
                if finished:
                    if sys.stdin.isatty() and sys.stdout.isatty():  # keep the result readable in the tab
                        print("\nNo longer polling. Ctrl-C or close the tab.", flush=True)
                        while True:
                            time.sleep(3600)
                    return 0
                time.sleep(self.args.interval)
        except KeyboardInterrupt:
            return 0


def fit(text, width):
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


def compact(j, width):
    """The lines of one job in the overview."""
    state, name = j["state"], clean(j["name"])
    col = COLORS.get(state, "")
    eta = f" · ETA {fmt_dur(j['eta_s'])}" if j["eta_s"] is not None and state in ("running", "orphan") else ""
    right = (" · ".join(x for x in (j["stage"], state) if x)) + eta
    name_w = min(len(name), width - 2)  # the name matters most: a long stage gets what is left
    lines = [
        f"{col}{ICONS.get(state, '●')}{RESET} {BOLD}{fit(name, name_w)}{RESET} {DIM}{fit(right, width - name_w - 3)}{RESET}"
    ]
    if j["total"]:
        pct, step = f" {j['frac'] * 100:3.0f}%", f"{j['step']}/{j['total']}"
        k = max(width - len(pct), 5)
        lines.append(f"{col}{'█' * round(j['frac'] * k)}{RESET}{DIM}{'░' * (k - round(j['frac'] * k))}{RESET}{pct}")
        detail = (
            [step] + [f"{k} {v}" for k, v in j["metrics"].items()] + ([f"upd {j['updated']}"] if j["updated"] else [])
        )
        lines.append(DIM + fit(" · ".join(detail), width) + RESET)
    if j["note"]:
        lines.append(col + fit(clean(j["note"]), width) + RESET)
    return lines


def overview(jobs, down, project):
    """Every job, as wide as the terminal."""
    width = max(shutil.get_terminal_size((80, 24)).columns - 1, 20)
    going = sum(j["state"] not in ENDED for j in jobs)
    lines = [
        f"{BOLD}Jobs{' · ' + project if project else ''}{RESET}  {DIM}{going} running · {time.strftime('%H:%M:%S')}{RESET}",
        "",
    ]
    for label, why in down.items():
        lines.append(f"{YELLOW}⚠ {label}: {fit(clean(why), width - len(label) - 4)}{RESET}")
    for j in jobs:
        lines += compact(j, width) + [""]
    if not jobs:
        lines.append(
            f"{DIM}no jobs{' in project ' + project if project else ''} (finished ones leave this view after a while; `jobwarden status --all` lists every job and its project){RESET}"
        )
    return "\n".join(lines)


def run_overview(args):
    if args.once:
        reader = Reader()
        jobs = reader.jobs(args.project, host=args.host)
        print(plain(overview(jobs, reader.down(), args.project)))
        return 3 if reader.down() else 0
    quit_on_close()
    notifier = Notifier(args.notify)
    live = Live(args.project, args.interval, args.host)
    tty, last = sys.stdout.isatty(), None
    if tty:
        sys.stdout.write("\033[?1049h\033[?25l")  # alternate screen, no cursor
    try:
        while True:
            jobs = live.jobs()
            for j in jobs:
                notifier.check(j)
            frame = overview(jobs, live.down(), args.project)
            if frame != last:  # in place, so it doesn't flash
                last = frame
                sys.stdout.write(
                    "\033[H" + plain(frame).replace("\n", "\033[K\n") + "\033[K\033[J" if tty else plain(frame) + "\n\n"
                )
                sys.stdout.flush()
            time.sleep(min(args.interval, 2))
    except KeyboardInterrupt:
        return 0
    finally:
        if tty:
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()


def json_line(label, jobs, reader):
    return json.dumps(
        {
            "version": 1,
            "host": label,
            "reachable": label not in reader.down_since,
            "error": reader.down().get(label),
            "jobs": [public(j) for j in jobs],
        }
    )


def run_stream(args):
    if args.names and not args.host:
        reader = Reader()
        try:
            for name in args.names:
                reader.host_of(name)
        except LookupError as e:
            print(e, file=sys.stderr)
            return 1
    wanted, include_hidden = set(args.names), bool(args.names)
    quit_on_close()
    notifier, lock, ended, finished = Notifier(args.notify), threading.Lock(), set(), threading.Event()

    def emit(live, label, jobs):
        jobs = [j for j in jobs if not wanted or j["name"] in wanted]
        with lock:
            for j in jobs:
                notifier.check(j)
                if j["state"] in ENDED:
                    ended.add(j["name"])
            try:
                print(json_line(label, jobs, live.reader), flush=True)
            except BrokenPipeError:  # whoever was reading has gone
                os._exit(0)
            if wanted and wanted <= ended:
                finished.set()

    Live(args.project, args.interval, args.host, include_hidden, emit)
    try:
        while not finished.wait(1):
            pass
        return 0
    except KeyboardInterrupt:
        return 0


def add_arguments(p, job_name):
    p.add_argument(
        "names", nargs="*", metavar="name", type=job_name, help="jobs to show in detail (default: all of them)"
    )
    p.add_argument("--project", help="without names: only this project's jobs")
    p.add_argument("--host", help="only this host (needed only for a name that exists on several)")
    p.add_argument("--json", action="store_true", help="for programs: one line of JSON per host at each read")
    p.add_argument("--config", help="with names: what is specific to a job (see above)")
    p.add_argument("--interval", type=float, default=10, help="seconds between reads (default 10)")
    p.add_argument("--once", action="store_true")
    p.add_argument("--notify", metavar="COMMAND", help="shell command to run on each event")


def run(args):
    if args.names and args.project or args.config and (not args.names or args.json) or args.json and args.once:
        print(
            "jobwarden watch: --project goes without names, --config with names and without --json, --once without --json",
            file=sys.stderr,
        )
        return 2
    if args.host:
        get_host(args.host)
    if args.json:
        return run_stream(args)
    if not args.names:
        return run_overview(args)
    try:
        w = Watch(args)
    except LookupError as e:
        print(e, file=sys.stderr)
        return 1
    return w.once() if args.once else w.loop()
