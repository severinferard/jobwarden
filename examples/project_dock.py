#!/usr/bin/env python3
"""A project's own Dock, when `jobwarden watch --project NAME` isn't enough: copy it into the project and change what it draws.
jobwarden gives the jobs, hoststats the machines.

    cmux new-pane --direction down --command "python3 project_dock.py"
"""

import os
import sys
import time

PROJECT, HOST = "demo", "box1"
sys.path[:0] = [os.path.expanduser("~/Code/jobwarden"), os.path.expanduser("~/Code/hoststats")]
import hoststats  # noqa: E402

import jobwarden  # noqa: E402


def frame(jobs):
    lines = []
    for j in jobs:
        lines.append(
            f"{j['name']:24} {j['state']:12} {j['step'] or 0}/{j['total'] or '?'}  {' '.join(f'{k} {v}' for k, v in j['metrics'].items()) or j['note'] or ''}"
        )
    try:
        for g in hoststats.gpus(jobwarden.ssh_command(HOST)):
            lines.append(
                f"GPU {g['name']:12} {g['util']:3.0f}%  {g['used'] / 1024:.1f}/{g['total'] / 1024:.0f} GB  {g['temp']:.0f}°C"
            )
    except (hoststats.Unavailable, jobwarden.Unreachable, jobwarden.UnknownHost) as e:
        lines.append(f"{HOST}: {e}")
    return "\n".join(lines) or "no jobs"


if __name__ == "__main__":
    if "--once" in sys.argv:
        print(frame(jobwarden.jobs(project=PROJECT)))
        sys.exit()
    live = jobwarden.live(project=PROJECT)
    while True:
        print("\033[2J\033[H" + frame(live.jobs()), flush=True)
        time.sleep(5)
