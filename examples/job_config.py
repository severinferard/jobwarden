"""A job's --config for the tab, with live GPU numbers from hoststats. Copy it next to the job and change what it shows.

jobwarden watch ft1 --config job_config.py
"""

import os
import sys

sys.path[:0] = [
    os.path.expanduser(os.environ.get("JOBWARDEN_REPO", "~/Code/jobwarden")),
    os.path.expanduser(os.environ.get("HOSTSTATS_REPO", "~/Code/hoststats")),
]
import hoststats  # noqa: E402

import jobwarden  # noqa: E402


def results(job):
    gpu = job["status"].get("gpu")
    if not gpu:
        return []
    g = next((g for g in hoststats.gpus(jobwarden.ssh_command(job["host"])) if g["uuid"] == gpu["uuid"]), None)
    return (
        [f"GPU {g['util']:.0f}% · {g['used'] / 1024:.1f}/{g['total'] / 1024:.0f} GB · {g['temp']:.0f}°C"] if g else []
    )
