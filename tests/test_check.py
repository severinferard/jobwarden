import os

from tests.base import Base, read_file, wait_for


class Check(Base):
    """jobwarden wait --check: wake up before something bad happens."""

    def wait(self, name, *args):
        w = self.waiter(name, "--check-every", "0.005", *args)
        out, _ = w.communicate(timeout=25)
        return w.returncode, out

    def test_exit_1_wakes_with_its_message_and_the_job_runs_on(self):
        self.jobrun("c1", "--cwd", self.tmp, "--", "echo '10:00:00 step 1/9 loss nan'; sleep 20")
        wait_for(lambda: "loss nan" in self.log("c1"), 10, "the log")
        rc, out = self.wait(
            "c1",
            "--check",
            'pwd > where; grep -q "loss nan" "$JOB_DIR/log" && { echo "loss is NaN in $JOB_NAME"; exit 1; }; exit 0',
        )
        self.assertEqual(rc, 6, out)
        self.assertIn("JOB c1 CHECK: loss is NaN in c1", out)
        self.assertEqual(
            os.path.realpath(read_file(os.path.join(self.tmp, "where")).strip()), os.path.realpath(self.tmp)
        )
        self.assertEqual(self.status("c1")["state"], "running")

    def test_exit_0_keeps_waiting_for_the_job(self):
        self.jobrun("c2", "--", "sleep 3")
        rc, out = self.wait("c2", "--check", "true")
        self.assertEqual(rc, 0, out)
        self.assertIn("DONE", out)

    def test_a_broken_check_says_so(self):
        self.jobrun("c3", "--", "sleep 20")
        rc, out = self.wait("c3", "--check", "no-such-check-xyz")
        self.assertEqual(rc, 6, out)
        self.assertIn("CHECK BROKEN: exit 127", out)

    def test_a_check_that_hangs_ends_up_saying_so(self):
        self.env["JOBWARDEN_SSH_TIMEOUT"] = "1"
        self.jobrun("c4", "--", "sleep 25")
        rc, out = self.wait("c4", "--check", "sleep 10")
        self.assertEqual(rc, 6, out)
        self.assertIn("without an answer", out)

    def test_a_short_job_still_gets_one_check_at_its_end(self):
        self.jobrun("c5", "--", "echo loss nan")
        self.wait_state("c5", "done")
        w = self.waiter("c5", "--check-every", "60", "--check", 'grep -q nan "$JOB_DIR/log" && { echo bad; exit 1; }; exit 0')
        out, _ = w.communicate(timeout=25)
        self.assertEqual(w.returncode, 6, out)
        self.assertIn("CHECK: bad (the job itself exited normally)", out)

    def test_no_check_on_a_failed_job(self):
        mark = os.path.join(self.tmp, "ran")
        self.jobrun("c7", "--", "exit 3")
        self.wait_state("c7", "failed")
        rc, out = self.wait("c7", "--check", f"touch {mark}; exit 1")
        self.assertEqual(rc, 1, out)
        self.assertFalse(os.path.exists(mark))

    def test_it_notifies(self):
        got = os.path.join(self.tmp, "got")
        self.jobrun("c6", "--", "sleep 20")
        rc, _ = self.wait(
            "c6", "--check", "echo bad; exit 1", "--notify", f'echo "$JOBWARDEN_EVENT|$JOBWARDEN_BODY" >> {got}'
        )
        self.assertEqual(rc, 6)
        self.assertEqual(read_file(got).strip(), "check|bad")
