import os
import signal
import subprocess

from tests.base import JOBWARDEN, Base, alive, wait_for


class Adopt(Base):
    def jk(self, *args):
        return subprocess.run([*JOBWARDEN, *args], env=self.env, capture_output=True, text=True, timeout=30)

    def orphan(self, name, *options, command="echo '10:00:00 step 1/9'; sleep 2; echo '10:00:01 step 2/9'; sleep 2"):
        self.jobrun(name, *options, "--", command)
        st = self.wait_state(name, "running")
        os.kill(st["pid"], signal.SIGKILL)
        wait_for(lambda: not alive(st["pid"]), 5, "the supervisor")
        return st

    def test_progress_is_tracked_again(self):
        st = self.orphan("a1")
        r = self.jk("adopt", "a1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(alive(st["job_pid"]))
        wait_for(lambda: (self.status("a1").get("progress") or {}).get("step") == 2, 10, "step 2")
        self.assertNotEqual(self.status("a1")["pid"], st["pid"])
        self.assertEqual(self.dock("a1")["state"] in ("running", "failed"), True)

    def test_without_checks_it_ends_failed_with_the_reason(self):
        self.orphan("a2")
        self.jk("adopt", "a2")
        st = self.wait_state("a2", "failed", "done")
        self.assertEqual(st["state"], "failed")
        self.assertIsNone(st["exit_code"])
        self.assertIn("exit code is unknown", st["reason"])

    def test_with_its_expected_file_it_ends_done(self):
        command = "sleep 3; echo x > out.txt"
        self.orphan("a3", "--cwd", self.tmp, "--expect", "out.txt", command=command)
        self.jk("adopt", "a3")
        st = self.wait_state("a3", "failed", "done")
        self.assertEqual(st["state"], "done", st["reason"])
        self.assertIn("exit code unknown", st["reason"])

    def test_a_job_with_a_supervisor_is_refused(self):
        self.jobrun("a4", "--", "sleep 10")
        self.wait_state("a4", "running")
        r = self.jk("adopt", "a4")
        self.assertEqual(r.returncode, 1)
        self.assertIn("still has its supervisor", r.stderr)

    def test_a_job_with_no_process_is_refused(self):
        st = self.orphan("a5", command="sleep 1")
        wait_for(lambda: not alive(st["job_pid"]), 10, "the job")
        r = self.jk("adopt", "a5")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no process left", r.stderr)

    def test_an_ended_job_has_nothing_to_adopt(self):
        self.jobrun("a6", "--", "true")
        self.wait_state("a6", "done")
        self.assertIn("already done", self.jk("adopt", "a6").stdout)

    def test_stop_works_after_adopting(self):
        st = self.orphan("a7", command="sleep 60")
        self.jk("adopt", "a7")
        wait_for(lambda: self.status("a7")["pid"] != st["pid"], 5, "the new supervisor")
        self.assertEqual(self.jk("stop", "a7", "--grace", "1").returncode, 0)
        self.assertEqual(self.status("a7")["state"], "stopped")
        wait_for(lambda: not alive(st["job_pid"]), 5, "the job")
