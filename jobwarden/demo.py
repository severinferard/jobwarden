"""jobwarden demo: a fake training run on this machine, to see the live view and a health check at work. Needs no GPU or SSH."""

import os
import shutil
import subprocess
import sys
import time

from .core import LOCAL
from .reader import ENDED, Reader
from .watch import overview

NAME = "jobwarden-demo"
TRAINING = """
import random, time
loss = 2.4
for step in range(1, 61):
    loss = float("nan") if step >= 42 else loss * random.uniform(0.93, 0.99)
    print(time.strftime("%H:%M:%S"), f"step {step}/60 loss {loss:.3f} 0.3s/step", flush=True)
    time.sleep(0.3)
"""
CHECK = '! grep -q "loss nan" "$JOB_DIR/log" || { echo "loss is NaN, stop this run"; exit 1; }'


def jobrun(*args):
    jobrun_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "jobrun.py")
    return subprocess.run([sys.executable, jobrun_file, *args], capture_output=True, text=True)


def run(args):
    jobs_dir = os.path.expanduser(f"~/.jobwarden/jobs/{NAME}")
    jobrun("stop", NAME, "--grace", "0")
    shutil.rmtree(jobs_dir, ignore_errors=True)
    started = jobrun("start", NAME, "--summary", "A fake training run", "--", sys.executable, "-c", TRAINING)
    if started.returncode != 0:
        sys.exit(started.stderr.strip())
    wait = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "jobwarden",
            "wait",
            NAME,
            "--host",
            LOCAL,
            "--interval",
            "0.5",
            "--check-every",
            "0.02",
            "--check",
            CHECK,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    reader = Reader()
    try:
        while wait.poll() is None:
            job = reader.read(LOCAL, [NAME])[NAME]
            lines = overview([job] if job else [], {}, None)
            print(
                f"\033[2J\033[H$ jobwarden watch\n\n{lines}\n\n$ jobwarden wait {NAME} --check '...'   # waiting in the background",
                flush=True,
            )
            if job and job["state"] in ENDED:
                break
            time.sleep(0.5)
        print("\n" + wait.communicate()[0].strip())
        jobrun("stop", NAME, "--grace", "0")
    finally:
        wait.kill()
        shutil.rmtree(jobs_dir, ignore_errors=True)
    print("Try it on your own job: jobwarden run train -- 'python train.py'")
    return 0
