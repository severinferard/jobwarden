# Design notes

Lessons from running long jobs on shared lab boxes, and what each one changed.

- **A viewer must not leave state behind.** A test run of a watcher once left a state file with a dead pid, which raised a false alarm later. Readers now save nothing: the record on the host is the only one.
- **When a watcher "hangs" overnight, check whether the laptop slept first** (`pmset -g log` on a Mac). A freeze blamed on `subprocess.run(timeout=)` with `ssh -J` didn't reproduce; it does leak the proxy child, though. So every command runs in its own process group with a hard timeout and temp files instead of pipes, which nothing left behind can hold open.
- **A healthy ping doesn't mean healthy SSH.** Banners can stall, and some networks block a source after a few new connections. So there's one shared connection per host, and a reconnect backoff shared by all readers. A failed command leaves that connection alone, since others may be using it.
- **Exit 0 isn't proof.** A crash in 36 s was once reported as finished because nothing looked at the result. Hence the recorded exit code, `--min-runtime` and `--expect`.
- **Containers outlive their clients.** Killing `docker run` leaves the container running, hence `--name "$JOB_CONTAINER"`.
