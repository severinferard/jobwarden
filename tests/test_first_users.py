import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.base import JOBRUN, JOBWARDEN, Base, alive, wait_for, write_exe


class FirstUsers(Base):
    """What three first-time testers found."""

    def jk(self, *args, **kw):
        return subprocess.run([*JOBWARDEN, *args], env=self.env, capture_output=True, text=True, timeout=30, **kw)

    def test_stop_gives_a_plain_job_sigterm_straight_away(self):
        self.jobrun("t1", "--", "sleep 60")
        self.wait_state("t1", "running")
        t0 = time.time()
        self.assertEqual(self.jk("stop", "t1", "--grace", "20").returncode, 0)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(self.status("t1")["exit_code"], -15)

    def test_a_trap_can_save_its_work_on_stop(self):
        self.jobrun("t2", "--", "trap 'echo saved; exit 0' TERM; echo up; while :; do sleep 0.2; done")
        wait_for(lambda: "up" in self.log("t2"), 10, "the trap")
        self.assertEqual(self.jk("stop", "t2", "--grace", "10").returncode, 0)
        self.assertIn("saved", self.log("t2"))

    def test_a_reservation_outlives_the_supervisor(self):
        self.jobrun("g1", "--gpu", "2080", "--", "sleep 30")
        st = self.wait_state("g1", "running")
        os.kill(st["pid"], signal.SIGKILL)
        wait_for(lambda: not alive(st["pid"]), 5, "the supervisor")
        r = self.jobrun("g2", "--gpu", "2080", "--", "true")
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertIn("reserved by jobrun job g1", r.stderr)
        os.killpg(st["job_pid"], signal.SIGKILL)
        wait_for(lambda: not alive(st["job_pid"]), 5, "the job")
        wait_for(lambda: self.jobrun("g3", "--gpu", "2080", "--", "true").returncode == 0, 5, "the card to be free")

    def test_a_reservation_is_kept_outside_the_home_folder(self):
        locks = self.env["JOBWARDEN_GPU_LOCKS"]
        self.jobrun("g4", "--gpu", "2060", "--", "sleep 3")
        self.wait_state("g4", "running")
        files = os.listdir(locks)
        self.assertEqual(len(files), 1)
        self.assertEqual(os.stat(os.path.join(locks, files[0])).st_mode & 0o777, 0o666)
        self.assertEqual(os.stat(locks).st_mode & 0o7777, 0o1777)

    def test_wait_wakes_on_an_orphan(self):
        self.env["JOBWARDEN_STALE"] = "1"
        self.jobrun("o1", "--", "sleep 30")
        st = self.wait_state("o1", "running")
        os.kill(st["pid"], signal.SIGKILL)
        w = self.waiter("o1")
        out, _ = w.communicate(timeout=20)
        self.assertEqual(w.returncode, 9, out)
        self.assertIn("jobwarden adopt o1", out)
        os.killpg(st["job_pid"], signal.SIGKILL)

    def test_wait_on_an_unreadable_status_ends_up_stalled(self):
        self.jobrun("u1", "--", "sleep 20")
        self.wait_state("u1", "running")
        st = self.status("u1")
        os.kill(st["pid"], signal.SIGKILL)
        with open(os.path.join(self.home, ".jobwarden", "jobs", "u1", "status.json"), "w") as f:
            f.write("{not json")
        w = self.waiter("u1", "--stall", "0.02")
        out, _ = w.communicate(timeout=20)
        self.assertEqual(w.returncode, 8, out)
        self.assertIn("unreadable", out)
        os.killpg(st["job_pid"], signal.SIGKILL)

    def test_wait_with_the_host_down_says_unreachable_in_time(self):
        write_exe(os.path.join(self.bin, "ssh"), "#!/bin/sh\necho refused >&2; exit 255\n")
        self.env.update(JOBWARDEN_BACKOFF="0.1")
        self.save_hosts({"down": {"ssh": "u@h"}})
        w = self.waiter("nothere", "--unreachable", "0.02")
        out, _ = w.communicate(timeout=20)
        self.assertEqual(w.returncode, 3, out)
        self.assertIn("not answering: down", out)

    def test_a_job_without_a_bar_says_why(self):
        self.jobrun("b1", "--", "echo no format here; sleep 5")
        wait_for(lambda: "no format" in self.log("b1"), 10, "the log")
        self.assertIn("No progress line seen yet", self.jk("watch", "b1", "--host", "lo", "--once").stdout)

    def test_output_has_no_colour_codes_in_a_pipe(self):
        self.jobrun("c1", "--", "sleep 3")
        self.wait_state("c1", "running")
        for args in (("watch", "c1", "--once"), ("watch", "--once")):
            self.assertNotIn("\x1b", self.jk(*args).stdout)

    def test_a_failing_notification_says_so(self):
        self.jobrun("n1", "--", "true")
        self.wait_state("n1", "done")
        w = subprocess.run(
            [*JOBWARDEN, "wait", "n1", "--notify", "exit 9"], env=self.env, capture_output=True, text=True, timeout=30
        )
        self.assertIn("--notify failed (exit 9)", w.stderr)

    def test_version_status_host_and_unknown_stream(self):
        self.assertTrue(self.jk("--version").stdout.startswith("jobwarden "))
        self.jobrun("v1", "--", "sleep 3")
        self.assertIn("on lo", self.jk("status").stdout)
        self.assertEqual(self.jk("watch", "nosuch", "--json").returncode, 1)

    def test_adopt_with_only_min_runtime_can_end_done(self):
        self.jobrun("a1", "--min-runtime", "1", "--", "sleep 4")
        st = self.wait_state("a1", "running")
        os.kill(st["pid"], signal.SIGKILL)
        wait_for(lambda: not alive(st["pid"]), 5, "the supervisor")
        self.jk("adopt", "a1")
        self.assertEqual(self.wait_state("a1", "done", "failed")["state"], "done")

    def test_a_refused_start_leaves_the_previous_record_alone(self):
        self.jobrun("r1", "--", "echo first")
        self.wait_state("r1", "done")
        self.jobrun("holder", "--gpu", "2080", "--", "sleep 10")
        self.wait_state("holder", "running")
        wait_for(lambda: self.jobrun("r1", "--gpu", "2080", "--", "true").returncode == 5, 5, "the refusal")
        self.assertIn("first", self.log("r1"))
        self.assertEqual(self.status("r1")["state"], "done")

    def test_stop_says_when_it_had_to_kill(self):
        self.jobrun("k1", "--", "trap '' TERM; echo up; while :; do sleep 0.2; done")
        wait_for(lambda: "up" in self.log("k1"), 10, "the trap")
        r = self.jk("stop", "k1", "--grace", "1")
        self.assertIn("so it was killed", r.stdout)

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("sudo"), "needs Linux and sudo")
    def test_a_reservation_works_across_real_unix_users(self):
        """The Linux rule that broke this (protected_regular) only applies to a second real user: CI creates `jwother`."""
        if subprocess.run(["sudo", "-n", "id", "jwother"], capture_output=True).returncode != 0:
            self.skipTest("no second user here")
        shared = tempfile.mkdtemp(prefix="jwshared", dir="/tmp")
        self.addCleanup(subprocess.run, ["sudo", "-n", "rm", "-rf", shared])
        os.chmod(shared, 0o755)
        jobrun = shutil.copy(JOBRUN, shared)
        os.chmod(jobrun, 0o755)
        locks = os.path.join(shared, "locks")
        smi = os.path.join(shared, "nvidia-smi")
        shutil.copy(os.path.join(self.bin, "nvidia-smi"), smi)
        os.chmod(smi, 0o755)
        os.chmod(self.tmp, 0o755)
        os.chmod(self.smi, 0o755)

        def start(name, *sudo):
            home = os.path.join(shared, "home-" + (sudo[-1] if sudo else "me"))
            prefix = ["sudo", "-n", "-u", "jwother"] if sudo else []
            subprocess.run([*prefix, "mkdir", "-p", home + "/.jobwarden"], check=True)
            env = [f"HOME={home}", f"JOBWARDEN_GPU_LOCKS={locks}", f"PATH={shared}:/usr/bin:/bin"]
            return subprocess.run(
                [
                    *prefix,
                    "env",
                    *env,
                    sys.executable,
                    jobrun,
                    "start",
                    name,
                    "--gpu",
                    "2080",
                    "--cwd",
                    "/tmp",
                    "--",
                    "sleep 3",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

        os.chmod(shared, 0o1777)
        first = start("a", "jwother")  # the other user creates the card's lock file
        self.assertEqual(first.returncode, 0, first.stderr)
        busy = start("b")
        self.assertEqual(busy.returncode, 5, busy.stderr)
        self.assertIn("reserved by jobrun job a of user jwother", busy.stderr)
        time.sleep(5)
        free = start("c")  # used to crash here with PermissionError
        self.assertEqual(free.returncode, 0, free.stderr)
