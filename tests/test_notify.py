import os
import subprocess
import time

from jobwarden import notify, reader
from tests.base import JOBWARDEN, Base, read_file


class Notifications(Base):
    """Per reader process, opt-in, a command of yours: jobwarden knows no terminal."""

    def setUp(self):
        super().setUp()
        self.got = os.path.join(self.tmp, "got")
        self.cmd = f'echo "$JOBWARDEN_EVENT|$JOBWARDEN_TITLE|$JOBWARDEN_BODY|$JOBWARDEN_JOB|$JOBWARDEN_HOST" >> {self.got}'

    def lines(self):
        return read_file(self.got).splitlines() if os.path.exists(self.got) else []

    def test_wait_runs_the_command_once_with_the_event(self):
        self.jobrun("a1", "--", "sleep 1; exit 3")
        w = self.waiter("a1", "--notify", self.cmd)
        w.communicate(timeout=20)
        self.assertEqual(w.returncode, 1)
        got = self.lines()
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].startswith("failed|a1 failed|exit code 3"), got)
        self.assertTrue(got[0].endswith("|a1|lo"), got)

    def test_wait_tells_about_a_job_already_over(self):
        self.jobrun("a2", "--", "true")
        self.wait_state("a2", "done")
        w = self.waiter("a2", "--notify", self.cmd)
        w.communicate(timeout=20)
        self.assertEqual(len(self.lines()), 1)

    def test_watch_does_not_repeat_a_job_already_over(self):
        self.jobrun("a3", "--", "true")
        self.wait_state("a3", "done")
        subprocess.run(
            [*JOBWARDEN, "watch", "a3", "--host", "lo", "--notify", self.cmd, "--interval", "0.2"],
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(self.lines(), [])

    def test_watch_notifies_what_it_sees_happen(self):
        self.jobrun("a4", "--", "sleep 2")
        subprocess.run(
            [*JOBWARDEN, "watch", "a4", "--host", "lo", "--notify", self.cmd, "--interval", "0.2"],
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual([x.split("|")[0] for x in self.lines()], ["done"])

    def test_other_readers_change_nothing(self):
        self.jobrun("a5", "--", "sleep 3")
        w = self.waiter("a5", "--notify", self.cmd)
        while w.poll() is None:
            subprocess.run([*JOBWARDEN, "status"], env=self.env, capture_output=True, timeout=20)
        self.assertEqual(len(self.lines()), 1)

    def test_nothing_notifies_unless_asked(self):
        self.jobrun("a6", "--", "true")
        self.watch("lo", "a6").wait(timeout=20)
        subprocess.run([*JOBWARDEN, "status"], env=self.env, capture_output=True, timeout=20)
        self.waiter("a6").communicate(timeout=20)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".cache/jobwarden/notify.log")))

    def test_failed_or_hanging_command_is_logged_and_harmless(self):
        self.jobrun("a7", "--", "true")
        w = self.waiter("a7", "--notify", "echo nope >&2; exit 9")
        out, _ = w.communicate(timeout=20)
        self.assertEqual(w.returncode, 0, out)
        with open(os.path.join(self.home, ".cache/jobwarden/notify.log")) as f:
            self.assertRegex(f.read(), r"failed \(exit 9\): nope: a7 finished")

    def test_events_between_states(self):
        def ev(prev, new):
            job = lambda fields: dict(reader.blank_job("x", "h"), **fields)  # noqa: E731
            old = notify.snapshot(job(prev)) if prev is not None else None
            return [e[0] for e in notify.events(old, notify.snapshot(job(new)), job(new))]

        run, stalled = {"state": "running", "last_at": time.time()}, {"state": "running", "last_at": time.time() - 7200}
        self.assertEqual(ev(run, stalled), ["stalled"])
        self.assertEqual(ev(stalled, stalled), [])
        self.assertEqual(ev(stalled, run), ["progressing"])
        cut = {"state": "unreachable", "unreachable_since": time.time() - 3600}
        self.assertEqual(ev({"state": "unreachable", "unreachable_since": time.time()}, cut), ["unreachable"])
        self.assertEqual(ev(cut, run), ["reachable"])
        self.assertEqual(ev(None, {"state": "done"}), [])
        self.assertEqual(ev({"state": "done"}, {"state": "done"}), [])
