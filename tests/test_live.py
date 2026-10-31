import json
import os
import subprocess
import sys
import time

from tests.base import JOBWARDEN, ROOT, Base, read_file, wait_for, write_exe


class Overview(Base):
    """jobwarden watch without names: every job, for a Dock."""

    def jk(self, *args, **kw):
        return subprocess.run([*JOBWARDEN, *args], env=self.env, capture_output=True, text=True, timeout=30, **kw)

    def test_once_shows_all_jobs_of_a_project(self):
        self.jobrun("o1", "--project", "p", "--", "echo '10:00:00 step 3/9 loss 0.5 2s/step'; sleep 20")
        self.jobrun("o2", "--project", "q", "--", "sleep 20")
        wait_for(lambda: self.status("o1")["progress"], 10, "progress")
        r = self.jk("watch", "--project", "p", "--once")
        self.assertEqual(r.returncode, 0, r.stderr)
        for want in ("o1", "3/9", "loss 0.5", "ETA 12s", "1 running"):
            self.assertIn(want, r.stdout)
        self.assertNotIn("o2", r.stdout)

    def test_it_picks_up_a_job_started_later_and_never_ends(self):
        out = os.path.join(self.tmp, "frames")
        with open(out, "w") as f:
            p = subprocess.Popen(
                [*JOBWARDEN, "watch", "--interval", "0.2"], env=self.env, stdin=subprocess.DEVNULL, stdout=f
            )
        self.procs.append(p)
        wait_for(lambda: "no jobs" in read_file(out), 10, "first frame")
        self.jobrun("late", "--", "true")
        wait_for(lambda: "late" in read_file(out) and "done" in read_file(out), 15, "the new job")
        time.sleep(0.5)
        self.assertIsNone(p.poll())

    def test_names_and_project_do_not_mix(self):
        self.assertEqual(self.jk("watch", "x", "--project", "p").returncode, 2)
        self.assertEqual(self.jk("watch", "--config", "c.py").returncode, 2)

    def test_notify_works_here_too(self):
        got = os.path.join(self.tmp, "got")
        self.jobrun("n1", "--", "sleep 2")
        p = subprocess.Popen(
            [*JOBWARDEN, "watch", "--interval", "0.2", "--notify", f"echo $JOBWARDEN_EVENT >> {got}"],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        self.procs.append(p)
        wait_for(lambda: os.path.exists(got) and "done" in read_file(got), 15, "the notification")


class JsonStream(Base):
    def stream(self, *args):
        p = subprocess.Popen(
            [*JOBWARDEN, "watch", "--json", "--interval", "0.2", *args],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.procs.append(p)
        return p

    def test_named_stream_ends_when_they_have(self):
        self.jobrun("s1", "--", "echo '10:00:00 step 1/2'; sleep 1")
        p = self.stream("s1")
        out, _ = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 0)
        lines = [json.loads(x) for x in out.splitlines()]
        self.assertEqual(lines[-1]["version"], 1)
        self.assertEqual((lines[-1]["host"], lines[-1]["reachable"]), ("lo", True))
        self.assertEqual(lines[-1]["jobs"][0]["state"], "done")
        self.assertNotIn("status", lines[-1]["jobs"][0])

    def test_a_dead_host_has_its_own_line_and_holds_back_nothing(self):
        write_exe(os.path.join(self.bin, "ssh"), "#!/bin/sh\nsleep 30\n")
        self.save_hosts({"slow": {"ssh": "u@h"}, "lo": {"ssh": "local"}})
        self.jobrun("s2", "--", "sleep 20")
        p = self.stream()
        t0 = time.time()
        first = json.loads(p.stdout.readline())
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(first["host"], "lo")
        self.assertEqual(first["jobs"][0]["name"], "s2")


class LiveApi(Base):
    def test_never_waits_and_shows_each_host_as_it_answers(self):
        write_exe(os.path.join(self.bin, "ssh"), "#!/bin/sh\nsleep 30\n")
        self.save_hosts({"slow": {"ssh": "u@h"}, "lo": {"ssh": "local"}})
        self.jobrun("l1", "--project", "p", "--", "sleep 20")
        self.jobrun("l2", "--project", "q", "--", "sleep 20")
        code = (
            "import sys, time; sys.path.insert(0, %r)\nimport jobwarden\nlive = jobwarden.live(project='p', interval=0.2)\n"
            "t = time.time(); live.jobs(); took = time.time() - t\n"
            "while not live.jobs() and time.time() - t < 8: time.sleep(0.1)\n"
            "print(took, [j['name'] for j in live.jobs()], time.time() - t)" % ROOT
        )
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=20)
        took, names, total = r.stdout.rsplit(maxsplit=2)[0].split(maxsplit=1)[0], r.stdout, r.stdout.split()[-1]
        self.assertLess(float(took), 0.1, r.stderr)
        self.assertIn("['l1']", names)
        self.assertLess(float(total), 5)

    def test_down_names_the_host(self):
        write_exe(os.path.join(self.bin, "ssh"), "#!/bin/sh\necho refused >&2; exit 255\n")
        self.save_hosts({"down": {"ssh": "u@h"}})
        code = (
            "import sys, time; sys.path.insert(0, %r)\nimport jobwarden\nlive = jobwarden.live(interval=0.2)\nt = time.time()\n"
            "while not live.down() and time.time() - t < 8: time.sleep(0.1)\nprint(live.down())" % ROOT
        )
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertIn("refused", r.stdout, r.stderr)
