"""Runs on the host through `python3 -`, with a call to main() added at the end. Prints one line of JSON, so nothing a job
writes in its log can be mistaken for the answer."""

import json
import os
import time

JOBS = os.path.expanduser("~/jobs")


def last_lines(path, count=8, width=300):
    try:
        with open(path, "rb") as f:
            f.seek(max(os.path.getsize(path) - 65536, 0))
            text = f.read().decode(errors="replace")
    except OSError:
        return []
    return [line[:width] for line in text.splitlines()[-count:]]


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def job(name):
    folder = os.path.join(JOBS, name)
    try:
        with open(os.path.join(folder, "status.json")) as f:
            text = f.read()
    except OSError:
        return None
    try:
        status = json.loads(text)
    except ValueError:
        status = None
    return {
        "status": status,
        "log": last_lines(os.path.join(folder, "log")),
        "job_alive": alive(status.get("job_pid")) if status else False,
        "cleared": os.path.exists(os.path.join(folder, ".cleared")),
    }


def main(names):
    """names: these jobs; None: every job on this host."""
    if names is None:
        try:
            names = sorted(n for n in os.listdir(JOBS) if not n.startswith("."))
        except OSError:
            names = []
    print(json.dumps({"now": time.time(), "jobs": {n: j for n, j in ((n, job(n)) for n in names) if j}}))
