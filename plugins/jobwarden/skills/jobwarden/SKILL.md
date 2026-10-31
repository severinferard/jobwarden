---
description: Run long jobs with jobwarden and get woken when they end or go wrong, instead of polling or writing a watcher script. Use when starting anything likely to run more than a few minutes without the user, such as training, rendering, batch processing, benchmarks or big downloads, on this machine or a remote host.
---

# Running long jobs with jobwarden

Don't poll a job, sleep in a loop or write a watcher. Start it with `jobwarden run`, start `jobwarden wait` in the background, and end your turn. When the job needs attention, the wait exits and that wakes you.

If `jobwarden` isn't installed, tell the user to run `uv tool install jobwarden` or `pipx install jobwarden`.

## 1. Launch

```sh
jobwarden run <name> [--host H] [--gpu NAME|any] [--wait-gpu] [--cwd DIR] [--summary TEXT] [--project NAME] \
    [--min-runtime SECONDS] [--expect FILE] -- '<command>'
```

- Without `--host`, the job runs on this machine. `jobwarden host list` shows the project's hosts. A new host needs the user's route: `jobwarden host add <name> --ssh user@addr`, then `jobwarden install <name>`.
- Quote the command as one argument.
- Set `--min-runtime` and `--expect` whenever you know what a healthy run looks like, because exit 0 alone doesn't prove it worked.
- Make the job print lines like `step 120/900 loss 0.85 1.2s/step`, with any word for `step`, or use tqdm. That gives the bar, the ETA and stall detection.
- Without `--gpu`, a job sees every card and reserves none. Pass `--gpu` to reserve one, always on a shared machine.
- A busy GPU is refused, with who holds it. Tell the user, or queue with `--wait-gpu`. Use `--force` only if the user says so, as the machine may be shared.
- For Docker, use `--name "$JOB_CONTAINER"`, plus `--gpus "device=$JOB_GPU"` with `--gpu`, and no `-d`.

## 2. Wait

Start this as a background command, one per job:

```sh
jobwarden wait <name> [--stall MINUTES] [--check 'COMMAND'] [--check-every MINUTES]
```

Set `--stall` above the job's slowest normal step (default 20 minutes). Add `--check` if a run can go wrong without failing, such as a loss going to NaN. It runs on the job's host, in its `--cwd`, with `$JOB_NAME` and `$JOB_DIR` set. It also runs once when the job exits normally. Exit 0 means fine, exit 1 wakes you with what it printed. Keep it read-only and under 30 s:

```sh
jobwarden wait train --check '! grep -q "loss nan" "$JOB_DIR/log" || { echo "loss is NaN"; exit 1; }'
```

Tell the user they can follow along with `jobwarden watch <name>`.

## 3. When the wait exits

It prints one line, `JOB <name> <EVENT>: ...`:

| Exit | Event | What to do |
|:--|:--|:--|
| 0 | `DONE` | Report the result and its key numbers |
| 1 | `FAILED` | Read the log and report what went wrong |
| 3 | `UNREACHABLE` | Check the host, read-only, and report |
| 4 | same name on several hosts | Wait again with `--host` |
| 5 | `CLEARED`, `NEVER APPEARED` | Check the name, and run from inside the project |
| 6 | `CHECK`, `CHECK BROKEN` | The job is usually still running; the line says if it has already exited. Diagnose, then tell the user |
| 7 | `STOPPED` | Someone stopped it; say so |
| 8 | `STALLED` | It may just be silent or slow. Diagnose before acting |
| 9 | `ORPHAN` | Its supervisor died and the job is running. `jobwarden adopt <name>`, then wait again |
| 2, 70 | no such line | A bad command or a jobwarden crash, not a job outcome |

A job's full log is at `~/.jobwarden/jobs/<name>/log` on its host, next to `status.json`. Read it with normal tools, over `ssh <host>` if it's remote, or use `jobwarden logs <name> -n 50` for the end of it.

## Rules

- Don't stop, restart or change a job without the user's go, and never touch a job someone else started. To stop one, use `jobwarden stop <name>`, not `kill`.
- After `CHECK`, `STALLED` or `ORPHAN` the job may still be running, and nothing is waiting for it. Start `jobwarden wait` again if it carries on.
- A job ending on a host added with `--temporary` may still be billing. Say so first. jobwarden never stops a pod.
