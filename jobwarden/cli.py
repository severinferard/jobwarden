"""Watch long-running jobs live, locally or over SSH, and wake your AI agent when they need attention (stdlib only). Hosts are per project, in the nearest .jobwarden/hosts.json
from the current folder upwards; without one, only this machine is known.

A --temporary host (a billed pod) accepts a new host key on first connection, since pods get a fresh one, and a job
ending on it is reported with a reminder that it may still be billing. Nothing here ever stops a pod, and only jobwarden stop
stops a job: that one job, started by jobwarden, and nothing else on the host.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import time
import traceback

from . import demo, wait, watch
from .core import (
    LOCAL,
    Transport,
    UnknownHost,
    Unreachable,
    config_path,
    get_host,
    is_local,
    load_hosts,
    save_hosts,
    ssh_command,
)
from .reader import ENDED, Reader, all_hosts, clean, no_hosts_note, no_job, public

JOBRUN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "jobrun.py")
REMOTE_JOBRUN = '"$HOME/.local/bin/jobrun"'
HASH_JOBRUN = "python3 -c " + shlex.quote(
    "import hashlib, os; "
    "print(hashlib.sha256(open(os.path.expanduser('~/.local/bin/jobrun'), 'rb').read()).hexdigest())"
)


def job_name(text):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", text):
        raise argparse.ArgumentTypeError(f"bad job name {text!r}: letters, digits, . _ - only")
    return text


def usage(message):
    print(f"jobwarden: {message}", file=sys.stderr)
    sys.exit(2)


def host_list(args):
    hosts = load_hosts()
    if not hosts:
        print(f"no hosts in {config_path()}")
    for name, h in hosts.items():
        route = (
            h.get("ssh", "")
            + (f" via {h['jump']}" if h.get("jump") else "")
            + (f" port {h['port']}" if h.get("port") else "")
        )
        gpus = ", ".join(h.get("gpus", {})) or "no GPU known"
        print(f"{name}: {route} · {gpus}" + (" · temporary" if h.get("temporary") else ""))


def host_add(args):
    hosts = load_hosts()
    if not args.local and not args.ssh:
        usage("--ssh user@addr or --local is required")
    h = {"ssh": "local" if args.local else args.ssh}
    if not args.local:
        h.update({k: v for k, v in (("jump", args.jump), ("port", args.port)) if v})
    extra = args.extra or ""
    if args.temporary:
        h["temporary"] = True
        if "StrictHostKeyChecking" not in extra:
            extra = (extra + " -o StrictHostKeyChecking=accept-new").strip()
    if extra:
        h["extra"] = extra
    if hosts.get(args.name, {}).get("gpus"):
        h["gpus"] = hosts[args.name]["gpus"]
    hosts[args.name] = h
    save_hosts(hosts)
    print(f"{args.name} saved in {config_path()}; next: jobwarden install {args.name}")


def host_rm(args):
    hosts = load_hosts()
    if args.name not in hosts:
        raise UnknownHost(f"unknown host {args.name}")
    Transport(args.name, hosts[args.name]).drop()
    del hosts[args.name]
    save_hosts(hosts)
    print(f"{args.name} removed from {config_path()} (the host and its jobs are untouched)")


def host_ssh_command(args):
    print(ssh_command(args.name))


def install(args):
    hosts = load_hosts()
    settings = get_host(args.host)
    t = Transport(args.host, settings)
    with open(JOBRUN) as f:
        code = f.read()
    # Atomic and stdin-only: no scp or rsync needed on the host, and a running jobrun keeps its old file.
    script = (
        'set -e; python3 -c "import sys; sys.exit(sys.version_info < (3, 10))" '
        '|| { echo "jobrun needs python3 >= 3.10 on this host" >&2; exit 1; }\n'
        'mkdir -p "$HOME/.local/bin"; cat > "$HOME/.local/bin/jobrun.tmp"; chmod +x "$HOME/.local/bin/jobrun.tmp"; '
        'mv "$HOME/.local/bin/jobrun.tmp" "$HOME/.local/bin/jobrun"'
    )
    rc, _, err = t.run(f"sh -c {shlex.quote(script)}", stdin_text=code)
    if rc != 0:
        sys.exit(f"install on {args.host} failed: {err.strip()}")
    rc, out, err = t.run(f"{REMOTE_JOBRUN} discover-gpus")
    if rc != 0:
        sys.exit(f"GPU discovery on {args.host} failed: {err.strip()}")
    settings["gpus"] = json.loads(out)
    hosts[args.host] = settings
    save_hosts(hosts)
    print(
        f"jobrun installed on {args.host}; GPUs: "
        + (", ".join(f"{k} = {v}" for k, v in settings["gpus"].items()) or "none")
    )


def status(args):
    reader = Reader()
    jobs = reader.jobs(args.project, include_hidden=args.all)
    down = reader.down()
    if down:
        print(f"not answering: {', '.join(down)}", file=sys.stderr)
    if args.json:
        print(json.dumps({"version": 1, "jobs": [public(j) for j in jobs], "down": down}, indent=2))
        return
    if not jobs:
        print(
            "no jobs"
            + (f" in project {args.project}" if args.project else "")
            + ("" if args.all else " (finished ones leave this view after a while; --all shows them)")
            + no_hosts_note(),
            file=sys.stderr,
        )
    for j in jobs:
        where = f"{j.get('step')}/{j.get('total')}" if j["total"] else ""
        print(" · ".join(str(x) for x in (j["name"], j["state"], where, j["stage"], j["note"], j["project"]) if x))


def clear(args):
    reader = Reader()
    for name in args.names:
        found, down = reader.locate(name)
        if not found:
            sys.exit(no_job(name, down))
        for label in found:
            reader.transport(label, all_hosts()[label]).run(f'touch "$HOME/.jobwarden/jobs/{name}/.cleared"')


def find_job(reader, name, host):
    """The one host that has this job, or exit with why not."""
    if host:
        get_host(host)
        return host
    try:
        return reader.host_of(name)
    except LookupError as e:
        sys.exit(str(e))


def logs(args):
    reader = Reader()
    label = find_job(reader, args.name, args.host)
    t = reader.transport(label, get_host(label))
    path = f'"$HOME/.jobwarden/jobs/{args.name}/log"'
    rc, out, err = t.run(f"wc -c < {path}; tail -n {args.lines} {path}")
    if rc != 0:
        sys.exit(no_job(args.name) if "No such file" in err else err.strip())
    size, _, text = out.partition("\n")
    sys.stdout.write(clean(text))
    sys.stdout.flush()
    offset = int(size.strip() or 0)
    while args.follow:
        job = reader.read(label, [args.name])[args.name]
        over = not job or job["state"] in ENDED
        rc, out, _ = t.run(f"tail -c +{offset + 1} {path}")
        if rc == 0 and out:
            sys.stdout.write(clean(out))
            sys.stdout.flush()
            offset += len(out.encode(errors="replace"))
        if over:
            return
        time.sleep(args.interval)


def stop(args):
    reader = Reader()
    label = find_job(reader, args.name, args.host)
    settings = get_host(label)
    t = reader.transport(label, settings)
    if not is_local(settings):
        warn_if_outdated(label, t)
    rc, out, err = t.run(f"{jobrun_command(settings)} stop {args.name} --grace {args.grace:g}", timeout=args.grace + 60)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return rc


def jobrun_command(settings):
    return f"{shlex.quote(sys.executable)} {shlex.quote(JOBRUN)}" if is_local(settings) else REMOTE_JOBRUN


def warn_if_outdated(label, t):
    """A host keeps the jobrun of its last install: say so if the repo has moved on."""
    with open(JOBRUN, "rb") as f:
        here = hashlib.sha256(f.read()).hexdigest()
    rc, out, _ = t.run(HASH_JOBRUN)
    if rc != 0 or out.strip() != here:
        print(
            f"warning: jobrun on {label} differs from this repo; `jobwarden install {label}` updates it", file=sys.stderr
        )


def run(argv):
    if argv[:1] in (["-h"], ["--help"]):
        print("jobwarden run <name> [--host H] [options below] -- <cmd>\n", flush=True)
        os.execv(sys.executable, [sys.executable, JOBRUN, "--help"])
    cut = argv.index("--") if "--" in argv else len(argv)
    p = argparse.ArgumentParser(prog="jobwarden run", add_help=False, allow_abbrev=False)
    p.add_argument("--host", default=LOCAL)
    known, own = p.parse_known_args(argv[:cut])
    if not own or own[0].startswith("-"):
        usage("jobwarden run <name> [--host H] [options] -- <cmd>; --help has the details")
    if "--" not in argv:
        usage(f"missing --: jobwarden run {own[0]} [options] -- {' '.join(own[1:]) or '<cmd>'}")
    settings = get_host(known.host)
    t = Transport(known.host, settings)
    if not is_local(settings):
        warn_if_outdated(known.host, t)
    command = [*own, *argv[cut:]]
    rc, out, err = t.run(f"{jobrun_command(settings)} start " + shlex.join(command))
    sys.stdout.write(out)
    sys.stderr.write(err)
    if rc == 0:
        print(f"Next: jobwarden watch {own[0]} · jobwarden wait {own[0]} · jobwarden logs {own[0]}")
    return rc


def parser():
    p = argparse.ArgumentParser(
        prog="jobwarden", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(metavar="command")
    sub.add_parser("run", help="start a job, on this machine unless --host (see jobwarden run --help)", add_help=False)
    for mod, name, helptext in (
        (watch, "watch", "follow jobs live: named ones in detail, or all of them"),
        (wait, "wait", "block until a job needs attention, then print one line and exit"),
    ):
        s = sub.add_parser(
            name, help=helptext, description=mod.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
        )
        mod.add_arguments(s, job_name)
        s.set_defaults(func=mod.run)
    s = sub.add_parser("demo", help="try it: a fake training run on this machine, with no GPU or SSH")
    s.set_defaults(func=demo.run)
    s = sub.add_parser("status", help="the jobs of all hosts, once")
    s.add_argument("--project", help="only this project's jobs")
    s.add_argument("--all", action="store_true", help="include those hidden from views")
    s.add_argument(
        "--json", action="store_true", help='for programs: {"version": 1, "jobs": [...], "down": {host: why}}'
    )
    s.set_defaults(func=status)
    s = sub.add_parser("clear", help="hide a job from views until its name runs again; the job carries on")
    s.add_argument("names", nargs="+", metavar="name", type=job_name)
    s.set_defaults(func=clear)
    s = sub.add_parser("logs", help="print a job's log")
    s.add_argument("name", type=job_name)
    s.add_argument("--host", help="only for a name that exists on several hosts")
    s.add_argument("-n", "--lines", type=int, default=50, help="last lines to print (default 50)")
    s.add_argument("-f", "--follow", action="store_true", help="keep printing until the job ends")
    s.add_argument("--interval", type=float, default=2, help="seconds between reads with --follow (default 2)")
    s.set_defaults(func=logs)
    s = sub.add_parser(
        "stop", help="stop one job: SIGTERM, then SIGKILL after --grace; nothing else on the host is touched"
    )
    s.add_argument("name", type=job_name)
    s.add_argument("--host", help="only for a name that exists on several hosts")
    s.add_argument("--grace", type=float, default=30, help="seconds before SIGKILL (default 30)")
    s.set_defaults(func=stop)
    s = sub.add_parser("install", help="copy jobrun to ~/.local/bin on a host, discover its GPUs into short names")
    s.add_argument("host")
    s.set_defaults(func=install)
    h = sub.add_parser("host", help="list, add or forget hosts")
    h.set_defaults(func=host_list)
    hs = h.add_subparsers(metavar="action")
    hs.add_parser("list").set_defaults(func=host_list)
    s = hs.add_parser("add", help="--ssh user@addr, or --local for this machine")
    s.add_argument("name")
    s.add_argument("--ssh")
    s.add_argument("--local", action="store_true")
    s.add_argument("--jump")
    s.add_argument("--port", type=int)
    s.add_argument("--temporary", action="store_true", help="a billed pod")
    s.add_argument("--extra", help="more ssh arguments")
    s.set_defaults(func=host_add)
    s = hs.add_parser("rm", help="only forgets the host; never touches it or its jobs")
    s.add_argument("name")
    s.set_defaults(func=host_rm)
    s = hs.add_parser("ssh-command", help="the ssh command for reading the host, e.g. for hoststats")
    s.add_argument("name")
    s.set_defaults(func=host_ssh_command)
    return p


def main():
    args = sys.argv[1:]
    if args[:1] == ["help"]:
        args = ["--help"]
    try:
        if args[:1] == ["run"]:
            sys.exit(run(args[1:]))
        p = parser()
        parsed = p.parse_args(args)
        if not hasattr(parsed, "func"):
            p.print_help()
            sys.exit(2)
        sys.exit(parsed.func(parsed))
    except UnknownHost as e:
        print(e, file=sys.stderr)
        sys.exit(2)
    except Unreachable as e:
        print(f"host unreachable: {e}", file=sys.stderr)
        sys.exit(3)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(70)
