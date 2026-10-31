<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/logo-dark.svg">
  <img src="docs/logo-light.svg" alt="" width="96">
</picture>

# jobwarden

**Watch long-running jobs live, on this machine or over SSH, and let your AI agent get woken when they need attention.**

![A fake training run in jobwarden's live view: the health check spots a NaN loss and wakes the agent](docs/demo.gif)

Start a job, see its progress as it runs, and walk away. It's built for AI coding agents like Claude Code, which shouldn't burn a session polling a training run or write a new watcher script each time: the agent waits on one command, which exits with a clear result as soon as the job finishes, fails, stalls, or fails your own health check. You follow along in the same live view.

```sh
jobwarden run train --host box1 --gpu 2080 -- 'python train.py'   # leave out --host to run on this machine
jobwarden watch train                                             # live progress: bar, ETA, metrics
jobwarden wait train --check 'python3 tools/check_training.py'    # blocks, then exits with what happened
```

- **Live progress.** Print one simple line and you get a bar, an ETA, metrics and stall detection, for one job or all of them.
- **Made for agents.** One blocking command, one line of output, one exit code per outcome, and JSON wherever a program wants it.
- **Local or remote.** The same commands work on your own machine and on hosts over SSH.
- **Jobs survive you.** A job on a remote host keeps running if your laptop sleeps or SSH drops.
- **No daemon, no server, no database.** The only record is a JSON file next to the job, so any viewer can come and go.
- **"Exit 0" isn't taken on trust.** A job that ran too briefly or didn't write its output counts as failed.
- **Safe on shared GPUs.** A busy card is refused, and a reservation stops two jobs picking the same one.
- **No dependencies.** It's standard-library Python 3.10+, on your machine and on the hosts.

It suits a laptop with a few SSH boxes, or rented pods. It isn't a cluster scheduler. It's been used from macOS with Linux hosts; other setups, and real rented pods, are untested.

## Install

```sh
uv tool install git+https://github.com/severinferard/jobwarden   # or: pipx install git+https://...
jobwarden demo                                                   # a fake training run: no GPU or SSH needed
```

Without any setup, jobs run on your own machine:

```sh
jobwarden run hello -- 'sleep 30; echo done'
jobwarden wait hello
```

To use a remote host, run these inside your project. Hosts are saved per project, in `.jobwarden/hosts.json`:

```sh
jobwarden host add box1 --ssh user@10.0.0.2          # optional: --jump user@gateway --port 2222
jobwarden install box1                               # copies jobrun to the host and names its GPUs
```

jobwarden finds the nearest `.jobwarden/hosts.json` from the current folder upwards. Outside a project it only knows your own machine, and says so. `ssh` to the host must already work without a password prompt, and the host needs `python3` 3.10+.

## For AI agents

The usual loop for a coding agent:

1. Launch with `jobwarden run`, setting `--min-runtime` and `--expect` whenever you know what a healthy run looks like.
2. Start `jobwarden wait <name>` as a background command, adding `--check` if a run can go wrong without failing.
3. When it exits, read the line and the exit code ([the full list](#waiting-for-a-job)). Use `jobwarden logs <name>` to diagnose.

In Claude Code, a background command's exit wakes the session, so the agent needs no polling at all. Any agent or script that can run a command and read its exit code can use the same loop, but how it gets woken depends on that tool. `examples/claude-code/job_launch.py` is a small Claude Code hook that reminds a session to do this after a launch.

## Running a job

```sh
jobwarden run train --host box1 --gpu 2080 --wait-gpu \
    --cwd ~/project --project demo --summary "Fine-tune v3 on the new dataset" \
    --min-runtime 600 --expect out/model.gguf \
    -- 'python train.py'
```

| Option | What it does |
|:--|:--|
| `--host` | where to run (default: this machine) |
| `--gpu` | a card's short name, `any` or a UUID. With no `--gpu`, the job sees no GPU |
| `--wait-gpu` | queue for a busy card instead of being refused |
| `--force` | use a busy card anyway |
| `--min-runtime SECONDS` | exit 0 sooner than this counts as failed |
| `--expect FILE` | exit 0 without this file, non-empty and newer than the start, counts as failed (repeatable) |
| `--project`, `--summary`, `--cwd` | a label for grouping, text for the viewer, and the working folder |

**Quote the command as one argument**, so that variables expand on the host.

**Progress:** print lines like these and you get a bar, an ETA, metrics and stall detection. The word `step` can be anything.

```
12:30:01 step 120/900 loss 0.85 1.2s/step
12:30:01 stage export
```

**Docker:** use both variables, and no `-d`. jobwarden already detaches the job, and it refuses a `docker run` it can see breaking these rules. Put them in any script your job runs too, since it can't check inside those.

```sh
jobwarden run train --host box1 --gpu 2080 -- \
    'docker run --rm --name "$JOB_CONTAINER" --gpus "device=$JOB_GPU" img python train.py'
```

## Following jobs

```sh
jobwarden watch train               # one job in detail; --once prints one frame
jobwarden watch --project demo      # every job, compact; picks up new ones and never ends
jobwarden status                    # a one-off list; --all includes hidden jobs
jobwarden logs train -n 50 -f       # finds the host for you; -f follows until the job ends
jobwarden stop train --grace 30     # SIGTERM, then SIGKILL after the grace
jobwarden clear train               # hide a job from views; the job carries on
```

Viewers are read-only, so closing one interrupts nothing. Finished jobs leave views after 60 minutes (`$JOBWARDEN_KEEP_MIN`), and nothing in `~/jobs` is ever deleted.

`stop` only reaches that job's process group and its container, and records the state `stopped`. It never stops a pod: a job ending on a host added with `--temporary` is reported as "may still be billing".

To show something specific to a job in `watch`, such as a results table or live GPU numbers, pass `--config job.py`. See `jobwarden watch --help` and `examples/job_config.py`.

## Waiting for a job

`jobwarden wait` blocks, prints one line, and exits with a code that says why. Start it as a background command from an AI agent or a script.

| Exit | Line starts with | Meaning |
|:--|:--|:--|
| 0 | `JOB train DONE:` | finished properly |
| 1 | `JOB train FAILED:` | see [how the outcome is decided](#how-the-outcome-is-decided) |
| 3 | `JOB train UNREACHABLE:` | host silent for `--unreachable` minutes (default 10) |
| 4 | `JOB train is on several hosts` | use `--host` |
| 5 | `JOB train CLEARED:` / `NEVER APPEARED:` | record gone, or not found after `--appear` minutes (default 10) |
| 6 | `JOB train CHECK:` / `CHECK BROKEN:` | your `--check` spoke up, or doesn't work |
| 7 | `JOB train STOPPED:` | someone ran `jobwarden stop` |
| 8 | `JOB train STALLED:` | no progress for `--stall` minutes (default 20) |
| 2, 70 | no such line | bad usage or unknown host; jobwarden itself crashed |

**Your own health check** catches what only your project can tell, such as a loss going to NaN, while there's still time to act:

```sh
jobwarden wait train --check-every 5 \
    --check '! grep -q "loss nan" "$JOB_DIR/log" || { echo "loss is NaN"; exit 1; }'
```

It runs on the job's host, in the job's folder, with `$JOB_NAME` and `$JOB_DIR` set, and has 30 s. Exit 0 means all is well, and exit 1 wakes you with whatever it printed. Anything else counts as a broken check, which wakes you too. The job keeps running either way. Keep checks read-only: jobwarden doesn't enforce that.

**Notifications:** `--notify` runs any command, so jobwarden doesn't depend on a particular terminal. It works on `wait` and `watch`; use one per job, or you'll get each alert twice.

```sh
jobwarden wait train --notify 'osascript -e "display notification \"$JOBWARDEN_BODY\" with title \"$JOBWARDEN_TITLE\""'
jobwarden wait train --notify 'notify-send "$JOBWARDEN_TITLE" "$JOBWARDEN_BODY"'
```

The command gets `$JOBWARDEN_TITLE`, `$JOBWARDEN_BODY`, `$JOBWARDEN_EVENT`, `$JOBWARDEN_JOB` and `$JOBWARDEN_HOST`.

**Staying awake:** nothing notices a job's end while no reader is running, and a sleeping laptop isn't running one. The job itself carries on if it's on another machine, and the record is waiting when you're back. On a Mac, `caffeinate -is jobwarden wait train` prevents idle sleep, though not a closed lid.

## Building your own dashboard

| | One reading | Live |
|:--|:--|:--|
| For people | `jobwarden status` | `jobwarden watch --project NAME` |
| For programs, any language | `jobwarden status --json` | `jobwarden watch --json` |
| In Python | `jobwarden.jobs()` | `jobwarden.live()` |

Often you need no code at all: run `jobwarden watch --project NAME` in a tmux pane or a terminal sidebar.

For a custom view in Python, `live()` reads in the background, so drawing never waits for a dead host:

```python
import time
import jobwarden

live = jobwarden.live(project="demo", interval=10)
while True:
    print(live.down())                 # {host: why} for hosts not answering
    for j in live.jobs():              # empty until the first hosts answer
        print(j["name"], j["state"], j["step"], j["total"], j["eta_s"], j["metrics"])
    time.sleep(2)
```

`jobwarden.jobs()` reads the hosts right now, and can wait about 40 s on a dead one. Import from where the package is installed; a `uv tool` install has its own environment, so a script may need `sys.path` set as in `examples/project_dock.py`.

Every job has these fields: `name`, `host`, `project`, `state` (`running`, `queued`, `orphan`, `unreachable`, `unreadable`, `done`, `failed`, `stopped`), `note`, `summary`, `gpu`, `step`, `total`, `frac`, `stage`, `metrics`, `eta_s`, `updated`, `last_at`, `start_at`, `ended_at`, `unreachable_since`, `temporary`, `cleared`, `log` (last lines), and in Python `status` (the raw `status.json`).

From another language, `jobwarden watch --json` prints one line per host at each read:

```json
{"version": 1, "host": "box1", "reachable": true, "error": null, "jobs": [{"name": "train", "state": "running", "step": 120, "total": 900}]}
```

## How it works

```
your machine                             host
────────────                             ────
jobwarden run ───── ssh ───────────────► jobrun ──► supervisor ──► your job
                                                        │              │
jobwarden watch ──┐                                     ▼              ▼
jobwarden wait  ──┼─ one shared ssh ───► reads   ~/jobs/<name>/status.json, log
jobwarden.live() ─┘   connection
```

`jobrun` is one file, copied to `~/.local/bin` on the host. `jobwarden run` warns if the copy is out of date. Each job gets a small supervisor that starts it, follows its log and writes `status.json`. Every reader then reads that file directly: nothing about jobs is saved on your machine.

All readers of a host share one SSH connection, every command has a hard timeout, and failed connections back off, so a dozen viewers don't look like an attack to a strict network. `docs/design-notes.md` explains the reasoning.

### How the outcome is decided

When the job's process ends, the supervisor records the first match:

| What happened | State |
|:--|:--|
| Ended by `jobwarden stop` | `stopped` |
| Killed by a signal, or a non-zero exit code | `failed` |
| Exit 0, but ran less than `--min-runtime` | `failed` |
| Exit 0, but an `--expect` file is missing, empty or older than the start | `failed` |
| Otherwise | `done` |

If the supervisor's heartbeat goes quiet for 60 s, readers report `orphan` when the job's process is still alive, and `failed` with an unknown exit code when it's gone. A reboot looks like this too, even if the job had in fact finished.

## What it doesn't catch

- **Wrong results with exit 0.** `--expect` only checks that a file exists, isn't empty and is new, and `--check` stops when the job does.
- **Healthy but silent jobs.** With no output for `--stall` minutes, `wait` reports `STALLED` and says no progress line was ever seen. Give such jobs a larger `--stall`.
- **Jobs started without jobwarden.** They hold no reservation, and only count as busy once `nvidia-smi` shows them.
- **Processes that escape.** `stop` can't reach a process that moved itself to its own session, or a container started by a script without `--name "$JOB_CONTAINER"`.
- **Containers killed from outside.** If the `docker run` client dies while its container carries on, the client's exit code is what gets recorded.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

The tests take about 2 minutes and use no network, real GPU or billable service: SSH is a stub or a throwaway `sshd` on localhost, and `HOME` is a temp folder.

`examples/project_dock.py` and `examples/job_config.py` also use `hoststats`, a separate package for CPU and GPU numbers that isn't published yet.

MIT licence: see `LICENSE`.
