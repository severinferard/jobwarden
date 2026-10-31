"""What a project's script calls (stdlib only). Add this repo to sys.path, then:

    import jobwarden
    for j in jobwarden.jobs(project="demo"):
        print(j["name"], j["state"], j["step"], j["total"], j["metrics"])

jobs() reads the hosts now, and returns when they have answered or timed out: a dead host can take about 40 s. A view that
must not wait uses live(), which reads in the background and always answers at once:

    live = jobwarden.live(project="demo", interval=10)
    while True:
        draw(live.jobs(), live.down())
        time.sleep(2)

Each dict has name, host, project, state (running, queued,
orphan, unreachable, unreadable, done, failed, stopped), step, total, frac, stage, eta_s, metrics (a dict), updated, last_at, note,
summary, gpu (the reserved card's short name),
start_at, ended_at, unreachable_since, temporary, cleared, status (its status.json, empty until read) and log (last lines).
Every job has all of them. The same data is in `jobwarden status --json`. down() says which hosts didn't answer.
"""

from .core import UnknownHost, Unreachable, ssh_command
from .reader import Live, Reader, all_hosts

__all__ = ["UnknownHost", "Unreachable", "down", "hosts", "jobs", "live", "ssh_command"]
_reader = Reader()


def jobs(project=None, include_hidden=False, host=None):
    """The jobs to show, read now, from all hosts or from one."""
    return _reader.jobs(project, include_hidden, host)


def live(project=None, interval=10, host=None):
    """Jobs read in the background every `interval` seconds: .jobs() and .down() never wait."""
    return Live(project, interval, host)


def down():
    """{host: why} for the hosts that didn't answer the last read."""
    return _reader.down()


def hosts():
    """The labels of the hosts jobs() reads: the project's hosts file, and "local" for this machine."""
    return list(all_hosts())
