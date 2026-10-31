# jobwarden

One tested way to launch, watch and wake on long jobs, across hosts. Stdlib Python 3.10+, no dependencies.

`~/jobs/<name>/status.json` on the host, written by `jobrun`, is the only record. Nothing about jobs is saved on your machine:
every reader reads the hosts directly, over each host's one shared SSH connection.

| Piece | Runs on | Does |
|:--|:--|:--|
| `jobwarden/jobrun.py` | the host (installed there as one file) | starts a job detached, records it in `~/jobs/<name>/{log,status.json}`, pins and reserves its GPU, decides done or failed |
| `jobwarden/cli.py` | this machine | the one command: `run`, `watch`, `wait`, `status`, `logs`, `stop`, `clear`, `host`, `install` (`jobwarden --help`) |
| `jobwarden/__init__.py` | this machine | what a script calls: `jobwarden.jobs()`, `jobwarden.live()`, `jobwarden.down()`, `jobwarden.hosts()`, `jobwarden.ssh_command()` |
| `jobwarden/reader.py`, `jobwarden/on_host.py` | this machine, host | reads the hosts (a small script sent to `python3 -`, answering in JSON) and interprets a job's status |
| `jobwarden/core.py` | this machine | hosts (the project's `.jobwarden/hosts.json`), transport |
| `jobwarden/wait.py` | this machine | `jobwarden wait`: waiter a Claude session (or a script) starts in the background: exits on done, failed, stalled, unreachable, or when your own `--check` says so |
| `jobwarden/watch.py` | this machine | `jobwarden watch`: follows jobs live, named ones in detail (with `--config`) or all of them. Closing it interrupts nothing |

**Notifications** (`jobwarden/notify.py`): per reader process, off by default. `jobwarden wait` and `jobwarden watch` take `--notify '<command>'`
and run it on each event they see themselves. The command gets `$JOBWARDEN_TITLE`, `$JOBWARDEN_BODY`, `$JOBWARDEN_EVENT`, `$JOBWARDEN_JOB`
and `$JOBWARDEN_HOST`, for any terminal or service:

```sh
jobwarden wait ft1 --notify 'cmux notify --title "$JOBWARDEN_TITLE" --body "$JOBWARDEN_BODY"'
jobwarden wait ft1 --notify 'osascript -e "display notification \"$JOBWARDEN_BODY\" with title \"$JOBWARDEN_TITLE\""'
jobwarden wait ft1 --notify 'notify-send "$JOBWARDEN_TITLE" "$JOBWARDEN_BODY"'
```
Dashboards and `jobwarden status` never notify. No such process running means no notification, though the record stays on the host.

**Claude Code**: `examples/claude-code/job_launch.py` is a small optional hook reminding a session to watch what it launched.

**Per project**: hosts live in the project, in the nearest `.jobwarden/hosts.json` from the current folder upwards (`$JOBWARDEN_HOSTS`
overrides it); without one, only this machine is known. A project keeps `.jobwarden/hosts.json`, and a dashboard script next to it if it wants one.

**Integrating**, from least to most work:

| | One-off | Live |
|:--|:--|:--|
| For people | `jobwarden status` | `jobwarden watch [--project NAME]` |
| For programs, in any language | `jobwarden status --json` | `jobwarden watch --json` |
| In Python | `jobwarden.jobs()` | `jobwarden.live()` |

A dashboard pane or a tmux pane can simply run `jobwarden watch --project NAME`. For a custom one in Python, `jobwarden.live()` reads in the background
and never makes you wait, so a dead host can't freeze the drawing; see `examples/project_dock.py`, which adds host stats from the separate
`hoststats` package. JSON output carries `"version": 1`; `jobwarden watch --help` shows the shape of its lines. jobwarden uses `nvidia-smi` only to name cards and check one is free before starting a job; live
numbers in a tab come from `--config`, see `examples/job_config.py`.

There is no daemon. If nothing is reading, nothing notices a job's end until something does; the record stays on the host.

## Use
```sh
uv tool install .                 # or pipx install .
jobwarden host add box1 --ssh user@10.0.0.2 --jump root@10.0.0.1
jobwarden install box1               # copies jobrun, discovers GPUs into short names; `jobwarden run` warns if it gets out of date

jobwarden run ft1 --host box1 --gpu 2080 --cwd ~/project --summary "what it is for" --project demo --min-runtime 600 --expect out/model.gguf -- \
    'docker run --rm --name "$JOB_CONTAINER" --gpus "device=$JOB_GPU" img python train.py'
jobwarden watch ft1 --once          # test: writes nothing
jobwarden wait ft1           # background: wakes a session; needs no tab
jobwarden wait ft1 --check 'python3 tools/check_training.py'   # also wakes it when your own check exits 1
jobwarden watch ft1                 # optional tab
```
Without `--host`, a job runs on this machine. `jobwarden watch` and `jobwarden wait` get the host from the job. `jobwarden --help`, `jobwarden run --help` and `jobwarden watch --help` have the details. Jobs print `HH:MM:SS step 120/513 loss 0.85 1.2s/step` and
optionally `HH:MM:SS stage gguf`. Project-specific text and results go in a small `--config` file, not in copied plumbing.

Pods: `jobwarden host add pod1 --ssh root@1.2.3.4 --port 22022 --temporary`. A job ending on a temporary host is reported
with "may still be billing: stop it?". Nothing in jobwarden ever stops a pod.

## Logs and stopping
```sh
jobwarden logs ft1 [-n 50] [-f]   # finds the host; -f follows until the job ends
jobwarden stop ft1 [--grace 30]   # SIGTERM, then SIGKILL after the grace
```
`jobwarden stop` stops that one job and nothing else on the host: its own process group, and its container. It records the state `stopped`,
and a queued job just leaves the queue. So a stop can reach a container, `docker run` needs `--name "$JOB_CONTAINER"`, which `jobrun`
requires of any docker command it can see; name it in the scripts a job runs too, as those can't be checked.

## How a job's outcome is decided
`jobrun` decides on the host, when the job's process ends, and records it in `status.json`. The first match wins:

| What happened | State |
|:--|:--|
| Killed by a signal, or a non-zero exit code | failed |
| Exit 0, but ran less than `--min-runtime` | failed |
| Exit 0, but an `--expect` file is missing, empty or older than the job's start | failed |
| Otherwise | done |

Without `--min-runtime` and `--expect`, success just means exit 0. If the supervisor's heartbeat is over 60 s old, readers report
`orphan` if the job's process is still alive, and `failed` with an unknown exit code if it's gone (a reboot looks like this).

## What wakes `jobwarden wait`
It prints one line starting `JOB <name> <EVENT>:` and exits:

| Exit code | Event |
|:--|:--|
| 0 | `DONE` |
| 1 | `FAILED` |
| 3 | `UNREACHABLE`: host not answering for `--unreachable` minutes (default 10) |
| 4 | same name on several hosts: use `--host` |
| 5 | `CLEARED`, or `NEVER APPEARED` after `--appear` minutes (default 10) |
| 6 | `CHECK`: your `--check` exited 1. `CHECK BROKEN`: it exited with anything else, or stopped answering |
| 7 | `STOPPED`: someone ran `jobwarden stop` |
| 8 | `STALLED`: no progress line for `--stall` minutes (default 20) |
| 2, 70 | bad usage or unknown host; jobwarden itself crashed. Not a job's outcome |

`--check '<command>'` is for what only your project can tell, such as a loss going to NaN. Every `--check-every` minutes (default 5)
while the job runs, it runs on the job's host, in the job's folder, with `$JOB_NAME` and `$JOB_DIR` set, and has 30 s:
```sh
jobwarden wait ft1 --check '! grep -q "loss nan" "$JOB_DIR/log" || { echo "loss is NaN"; exit 1; }'
```
It only wakes you: the job carries on. Keep it read-only, since jobwarden doesn't enforce that.

## What it does not catch
- Wrong results with exit 0: `--expect` only checks that a file exists, isn't empty and is new, and `--check` stops when the job does.
- A healthy job printing nothing: the stall timer falls back to the last log write, so give it a larger `--stall`.
- Jobs started without `jobrun`: they aren't reserved, only seen through nvidia-smi once they hold memory.
- `docker run` killed from outside while its container keeps running: the client's exit code is what is recorded.
- A process a job moved to its own session or group, or a container started by a script without `--name "$JOB_CONTAINER"`: `jobwarden stop` can't reach it.
- A job that survives its supervisor (`orphan` in the tab) has no recorded exit code.
- A laptop going to sleep: a remote job continues, watching pauses, and `jobwarden watch` says so afterwards. `caffeinate -is jobwarden wait ft1` keeps a Mac awake while it waits, short of a closed lid or an empty battery.

## When a job leaves `jobwarden.jobs()`
Finished: 60 min ($JOBWARDEN_KEEP_MIN) after it ended. Host not answering: a running process keeps showing its last read,
marked unreachable, and hides it after 60 min. `jobwarden clear <name>` hides a job until its name runs again. Hiding only
affects views: `jobwarden wait` and `jobwarden status --all` still see them. `~/jobs` is never cleaned up by jobwarden.

## Settings
`$JOBWARDEN_KEEP_MIN` (above), `$JOBWARDEN_HOSTS` (a hosts file instead of the project's). `--stall` and `--unreachable` belong to `jobwarden wait`, and its
notifications use the same numbers. The tests also shorten `$JOBWARDEN_SSH_TIMEOUT`, `$JOBWARDEN_BACKOFF`, `$JOBWARDEN_STALE`,
`$JOBWARDEN_HEARTBEAT` and `$JOBWARDEN_GPU_POLL`, and move `$JOBWARDEN_SSH_DIR`.

## Tests
`python3 -m unittest discover -s tests -t .` (about 2 min). No network, no real GPU, nothing billable: SSH is a stub or a
throwaway sshd on 127.0.0.1 (custom port, no docker, temporary: a stand-in for a pod), and HOME is a temp dir.

Why it works this way: `docs/design-notes.md`.
