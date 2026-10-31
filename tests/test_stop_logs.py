import json
import os
import signal
import subprocess
import sys
import time

from tests.base import JOBWARDEN, UUID_A, Base, alive, load_jobrun, read_file, wait_for, write_exe


class Stop(Base):
    def jk(self, *args):
        return subprocess.run([*JOBWARDEN, *args], env=self.env, capture_output=True, text=True, timeout=60)

    def test_stops_that_job_and_records_it(self):
        self.jobrun("s1", "--", "echo started; sleep 60")
        self.jobrun("other", "--", "sleep 60")
        pid = self.wait_state("s1", "running")["job_pid"]
        wait_for(lambda: "started" in self.log("s1"), 10, "the log")
        r = self.jk("stop", "s1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stopped", r.stdout)
        st = self.status("s1")
        self.assertEqual(st["state"], "stopped")
        wait_for(lambda: not alive(pid), 5, "the job")
        self.assertTrue(alive(self.wait_state("other", "running")["job_pid"]))
        self.assertEqual(self.dock("s1")["state"], "stopped")

    def test_a_job_ignoring_sigterm_gets_sigkill_after_the_grace(self):
        self.jobrun("s2", "--", "trap '' TERM; echo up; while :; do sleep 1; done")
        pid = self.wait_state("s2", "running")["job_pid"]
        wait_for(lambda: "up" in self.log("s2"), 10, "the trap")
        r = self.jk("stop", "s2", "--grace", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.status("s2")["state"], "stopped")
        wait_for(lambda: not alive(pid), 5, "the job")

    def test_a_queued_job_leaves_the_queue(self):
        self.set_gpus([(UUID_A, "RTX 2080", 5000)], [(UUID_A, 1, "x", 4900)])
        self.jobrun("s3", "--gpu", "2080", "--wait-gpu", "--", "echo ran")
        self.wait_state("s3", "queued")
        self.assertEqual(self.jk("stop", "s3").returncode, 0)
        self.assertEqual(self.status("s3")["state"], "stopped")
        self.assertNotIn("ran", self.log("s3") if os.path.exists(os.path.join(self.home, "jobs/s3/log")) else "")

    def test_without_a_supervisor_it_stops_the_job_itself(self):
        self.jobrun("s4", "--", "sleep 60")
        st = self.wait_state("s4", "running")
        os.kill(st["pid"], signal.SIGKILL)
        wait_for(lambda: self.jobrun("s4x", "--", "true"), 1)
        time.sleep(0.5)
        r = self.jk("stop", "s4", "--grace", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.status("s4")["state"], "stopped")
        wait_for(lambda: not alive(st["job_pid"]), 5, "the job")

    def test_a_recycled_pid_is_left_alone(self):
        victim = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.procs.append(victim)
        jr = load_jobrun()
        self.jobrun("s5", "--", "true")
        self.wait_state("s5", "done")
        path = os.path.join(self.home, "jobs/s5/status.json")
        st = self.status("s5")
        st.update(state="running", job_pid=victim.pid)
        with open(path, "w") as f:
            json.dump(st, f)
        if sys.platform == "linux":
            self.assertEqual(self.jk("stop", "s5", "--grace", "0").returncode, 0)
            self.assertTrue(alive(victim.pid))
        self.assertTrue(jr.own_process(victim.pid, "s5") or sys.platform == "linux")

    def test_an_ended_job_has_nothing_to_stop(self):
        self.jobrun("s6", "--", "true")
        self.wait_state("s6", "done")
        r = self.jk("stop", "s6")
        self.assertEqual(r.returncode, 0)
        self.assertIn("already done", r.stdout)
        self.assertEqual(self.status("s6")["state"], "done")

    def test_unknown_job(self):
        r = self.jk("stop", "nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no job nope", r.stderr)

    def test_the_container_is_stopped_by_name(self):
        calls = os.path.join(self.tmp, "docker-calls")
        write_exe(
            os.path.join(self.bin, "docker"),
            f'#!/bin/sh\necho "$@" >> {calls}\ncase "$1" in run) exec sleep 60;; esac\n',
        )
        self.jobrun("s7", "--", 'docker run --rm --name "$JOB_CONTAINER" img')
        self.wait_state("s7", "running")
        wait_for(lambda: os.path.exists(calls), 10, "docker")
        self.assertEqual(self.jk("stop", "s7", "--grace", "1").returncode, 0)
        self.assertIn("stop --time 1 job-s7", read_file(calls))

    def test_docker_without_a_known_name_is_refused(self):
        r = self.jobrun("s8", "--", "docker run --rm img")
        self.assertEqual(r.returncode, 2)
        self.assertIn("$JOB_CONTAINER", r.stderr)
        self.assertEqual(self.jobrun("s8b", "--force", "--", "docker run --rm img").returncode, 0)

    def test_waiter_says_stopped(self):
        self.jobrun("s9", "--", "sleep 60")
        self.wait_state("s9", "running")
        w = self.waiter("s9")
        time.sleep(0.5)
        self.jk("stop", "s9", "--grace", "1")
        out, _ = w.communicate(timeout=20)
        self.assertEqual(w.returncode, 7, out)
        self.assertIn("JOB s9 STOPPED", out)


class Logs(Base):
    def jk(self, *args):
        return subprocess.run([*JOBWARDEN, *args], env=self.env, capture_output=True, text=True, timeout=30)

    def test_last_lines_without_terminal_codes(self):
        self.jobrun("l1", "--", r"printf 'one\ntwo\n\033[2Jthree\n'")
        self.wait_state("l1", "done")
        r = self.jk("logs", "l1", "-n", "2")
        self.assertEqual(r.stdout, "two\nthree\n", r.stderr)

    def test_follow_ends_with_the_job(self):
        self.jobrun("l2", "--", "echo first; sleep 2; echo second")
        r = self.jk("logs", "l2", "-f", "--interval", "0.2")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.count("first"), 1, r.stdout)
        self.assertEqual(r.stdout.count("second"), 1, r.stdout)

    def test_unknown_job(self):
        r = self.jk("logs", "nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no job nope", r.stderr)


class JobFolder(Base):
    def test_status_says_where_the_files_are(self):
        self.jobrun("d1", "--", "echo hello")
        self.wait_state("d1", "done")
        folder = self.dock("d1")["dir"]
        self.assertEqual(os.path.realpath(folder), os.path.realpath(os.path.join(self.home, "jobs", "d1")))
        with open(os.path.join(folder, "log")) as f:
            self.assertEqual(f.read().strip(), "hello")


class Friendly(Base):
    def jk(self, *args, env=None, cwd=None):
        return subprocess.run(
            [*JOBWARDEN, *args], env=env or self.env, cwd=cwd, capture_output=True, text=True, timeout=30
        )

    def test_outside_a_project_it_says_so(self):
        env = {k: v for k, v in self.env.items() if k != "JOBWARDEN_HOSTS"}
        r = self.jk("status", env=env, cwd=self.tmp)
        self.assertIn("no .jobwarden/hosts.json", r.stderr)
        self.assertIn("no jobs", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertNotIn("no .jobwarden/hosts.json", self.jk("status").stderr)
        self.jobrun("here", "--", "sleep 5")
        self.assertEqual(self.jk("status", env=env, cwd=self.tmp).stderr, "", "a job was found: nothing to explain")

    def test_a_forgotten_separator_is_one_line(self):
        r = self.jk("run", "x", "sleep", "5")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stderr.strip(), "jobwarden: missing --: jobwarden run x [options] -- sleep 5")

    def test_launch_says_what_comes_next(self):
        r = self.jk("run", "n1", "--", "true")
        self.assertIn("Next: jobwarden watch n1 · jobwarden wait n1 · jobwarden logs n1", r.stdout)

    def test_waiter_explains_a_silent_job_and_names_no_tool(self):
        self.jobrun("q1", "--", "sleep 30")
        w = self.waiter("q1", "--stall", "0.01")
        out, _ = w.communicate(timeout=20)
        self.assertEqual(w.returncode, 8, out)
        self.assertIn("no progress line ever seen", out)
        self.assertNotIn("skill", out)

    def test_empty_views_point_to_the_list(self):
        self.assertIn("--all", self.jk("watch", "--project", "typo", "--once").stdout)
        self.assertIn("status --all", self.jk("watch", "nope", "--once").stderr)
