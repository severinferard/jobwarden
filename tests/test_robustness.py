import os
import signal
import socket
import subprocess
import sys
import time

from jobwarden import core
from tests.base import JOBWARDEN, Base, alive, read_file, wait_for, write_exe


class ReadersSaveNothing(Base):
    """--once left a state file with a dead pid, so the waiter said WATCHER GONE. There are no such files any more."""

    def test_reading_leaves_nothing_on_the_mac(self):
        self.jobrun("j1", "--", "sleep 5")
        self.assertEqual(self.watch_once("lo", "j1").returncode, 0)
        self.assertEqual(self.dock("j1")["state"], "running")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".cache/cmux-jobs")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".cache/jobwarden/jobs")))

    def test_waiter_on_an_unknown_job_says_so(self):
        w = self.waiter("j1x", "--appear", "0.01")
        out, _ = w.communicate(timeout=20)
        self.assertEqual(w.returncode, 5, out)
        self.assertIn("NEVER APPEARED", out)

    def test_waiter_needs_nothing_else_running(self):
        self.jobrun("j1c", "--", "sleep 2")
        w = self.waiter("j1c")
        out, _ = w.communicate(timeout=30)
        self.assertEqual(w.returncode, 0, out)
        self.assertIn("DONE", out)


class HardTimeouts(Base):
    """The watcher froze 02:15-05:17 and 04:17-05:17. The fix (bc36159) blamed subprocess.run(timeout=) never returning
    with ssh -J; that doesn't reproduce (test_old_way_times_out_but_leaks_the_proxy), and pmset's log has the Mac entering
    sleep at 02:15:47 and 04:17:13 and waking at 05:17:29, so it was most likely Incident 4. Kept: hard timeouts that
    can't be held open, and no leaked proxies."""

    def stub(self):
        """Behaves like ssh -J on a dead route: leaves a child holding stdout/stderr, then hangs."""
        path = os.path.join(self.bin, "hang-ssh")
        self.childpid = os.path.join(self.tmp, "childpid")
        write_exe(
            path,
            f"""
            #!/bin/sh
            sleep 300 &
            echo $! > {self.childpid}
            exec sleep 300
        """,
        )
        return path

    def test_old_way_times_out_but_leaks_the_proxy(self):
        """What is measurable about the old code: it does return, and it leaves the proxy behind."""
        path = self.stub()
        t0 = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run([path], capture_output=True, timeout=0.5)
        self.assertLess(time.time() - t0, 5)
        child = int(read_file(self.childpid))
        self.assertTrue(alive(child))
        os.kill(child, signal.SIGKILL)

    def test_real_ssh_jump_to_tarpit_leaves_no_proxy(self):
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        port = srv.getsockname()[1]
        rc, _, _ = core.run_bounded(
            ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-J", f"u@127.0.0.1:{port}", "u@127.0.0.1", "true"], 2
        )
        self.assertIsNone(rc)
        pat = f"ssh.*-p {port}.*-W"
        wait_for(lambda: not subprocess.run(["pgrep", "-f", pat], capture_output=True).stdout, 5, "proxy to die")

    def test_run_bounded_returns_on_time_and_kills_the_group(self):
        path = self.stub()
        t0 = time.time()
        rc, _, _ = core.run_bounded([path], 0.5)
        self.assertIsNone(rc)
        self.assertLess(time.time() - t0, 3)
        child = int(read_file(self.childpid))
        wait_for(lambda: not alive(child), 5, "proxy child to die")

    def test_daemon_in_its_own_session_cannot_hold_it_either(self):
        path = os.path.join(self.bin, "daemon-ssh")
        pidfile = os.path.join(self.tmp, "daemonpid")
        write_exe(
            path,
            f'''
            #!{sys.executable}
            import os, sys, time
            if os.fork() == 0:
                os.setsid()
                open("{pidfile}", "w").write(str(os.getpid()))
                time.sleep(30)
                os._exit(0)
            print("hello")
        ''',
        )
        t0 = time.time()
        rc, out, _ = core.run_bounded([path], 5)
        self.assertEqual((rc, out.strip()), (0, "hello"))
        self.assertLess(time.time() - t0, 3)
        os.kill(int(wait_for(lambda: os.path.exists(pidfile) and read_file(pidfile))), signal.SIGKILL)

    def test_watcher_keeps_reporting_while_ssh_hangs(self):
        path = self.stub()
        os.symlink(path, os.path.join(self.bin, "ssh"))
        self.env.update(JOBWARDEN_SSH_TIMEOUT="0.5", JOBWARDEN_BACKOFF="0.1")
        self.save_hosts({"dead": {"ssh": "u@h", "jump": "u@j"}})
        out = os.path.join(self.tmp, "frames")
        with open(out, "w") as f:
            p = subprocess.Popen(
                [*JOBWARDEN, "watch", "jx", "--host", "dead", "--interval", "0.2"],
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.STDOUT,
            )
        self.procs.append(p)
        wait_for(lambda: read_file(out).count("cannot reach dead") >= 2, 20, "the watcher still refreshing")


class SharedConnection(Base):
    """Banner timeouts while ping worked; a fresh connection per refresh made it fragile."""

    def test_tarpit_banner_is_unreachable_not_a_freeze(self):
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)  # accepts, never sends a banner, as the box did
        self.env.update(JOBWARDEN_SSH_TIMEOUT="1")
        self.save_hosts({"slow": {"ssh": "u@127.0.0.1", "port": srv.getsockname()[1], "extra": "-F /dev/null"}})
        t0 = time.time()
        r = self.watch_once("slow", "j")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("cannot reach", r.stdout)
        self.assertLess(time.time() - t0, 10)

    def test_failed_connects_back_off(self):
        """Each attempt is a new connection, which the lab router punishes: they must slow down, across watchers."""
        path = os.path.join(self.bin, "ssh")
        count = os.path.join(self.tmp, "attempts")
        write_exe(
            path,
            f"""
            #!/bin/sh
            case "$*" in *" -M "*) echo x >> {count}; echo "kex_exchange_identification: banner" >&2; exit 255;; esac
            exit 255
        """,
        )
        self.env.update(JOBWARDEN_BACKOFF="30")
        self.save_hosts({"down": {"ssh": "u@h"}})
        rs = [self.watch_once("down", "j") for _ in range(4)]
        self.assertEqual([r.returncode for r in rs], [3] * 4)
        self.assertEqual(len(read_file(count).splitlines()), 1)
        self.assertIn("next in", rs[-1].stdout)


class CommandFailures(Base):
    """A failing command must not turn into a burst of new connections."""

    def test_a_connection_that_fails_every_command_backs_off(self):
        count = os.path.join(self.tmp, "attempts")
        write_exe(
            os.path.join(self.bin, "ssh"),
            f"""
            #!/bin/sh
            case "$*" in
              *" -M "*) echo x >> {count}; touch {self.env["JOBWARDEN_SSH_DIR"]}/flaky; exit 0;;
              *"-O check"*) exit 1;;
              *"-O exit"*) exit 0;;
            esac
            echo "mux broke" >&2; exit 255
        """,
        )
        self.env.update(JOBWARDEN_BACKOFF="30")
        self.save_hosts({"flaky": {"ssh": "u@h"}})
        for _ in range(4):
            self.assertEqual(self.watch_once("flaky", "j").returncode, 3)
        self.assertEqual(len(read_file(count).splitlines()), 1)


class JobOutcome(Base):
    """A test run that crashed in 36 s was reported done: nothing checked the exit code."""

    def test_crash_is_failed_with_its_exit_code(self):
        self.jobrun("c", "--", "echo 'HTTP 400 on every request'; exit 7")
        st = self.wait_state("c", "done", "failed")
        self.assertEqual((st["state"], st["exit_code"]), ("failed", 7))
        self.assertIn("exit code 7", st["reason"])

    def test_crash_reaches_the_waiter_and_dock(self):
        self.jobrun("c2", "--", "echo 'HTTP 400 on every request'; exit 7")
        d = wait_for(lambda: (x := self.dock("c2")) and x.get("state") == "failed" and x, 15, "failed")
        self.assertIn("HTTP 400", d["note"])
        w = self.waiter("c2")
        out, _ = w.communicate(timeout=10)
        self.assertEqual(w.returncode, 1, out)
        self.assertIn("exit code 7", out)

    def test_too_short_run_is_failed(self):
        self.jobrun("s", "--min-runtime", "600", "--", "true")
        st = self.wait_state("s", "done", "failed")
        self.assertEqual((st["state"], st["exit_code"]), ("failed", 0))
        self.assertIn("--min-runtime", st["reason"])

    def test_long_enough_run_is_done(self):
        self.jobrun("s2", "--min-runtime", "1", "--", "sleep 2")
        self.assertEqual(self.wait_state("s2", "done", "failed")["state"], "done")

    def test_missing_output_is_failed(self):
        self.jobrun("e", "--cwd", self.tmp, "--expect", "out.gguf", "--", "true")
        st = self.wait_state("e", "done", "failed")
        self.assertEqual(st["state"], "failed")
        self.assertIn("missing: out.gguf", st["reason"])

    def test_empty_output_is_failed(self):
        self.jobrun("e2", "--cwd", self.tmp, "--expect", "o", "--", "touch o")
        self.assertIn("empty", self.wait_state("e2", "done", "failed")["reason"])

    def test_output_left_by_an_earlier_run_is_failed(self):
        path = os.path.join(self.tmp, "old")
        with open(path, "w") as f:
            f.write("x")
        os.utime(path, (time.time() - 3600,) * 2)
        self.jobrun("e3", "--cwd", self.tmp, "--expect", "old", "--", "true")
        self.assertIn("not written by this run", self.wait_state("e3", "done", "failed")["reason"])

    def test_output_written_is_done(self):
        self.jobrun("e4", "--cwd", self.tmp, "--expect", "o", "--", "echo x > o")
        self.assertEqual(self.wait_state("e4", "done", "failed")["state"], "done")

    def test_killed_job_is_failed(self):
        self.jobrun("k", "--", "sleep 30")
        st = self.wait_state("k", "running")
        os.kill(st["job_pid"], signal.SIGKILL)
        st = self.wait_state("k", "done", "failed")
        self.assertEqual((st["state"], st["exit_code"]), ("failed", -9))

    def test_command_not_found_is_failed(self):
        self.jobrun("n", "--", "no-such-command-xyz")
        self.assertEqual(self.wait_state("n", "done", "failed")["exit_code"], 127)

    def test_argv_form_not_found_is_failed_and_reported(self):
        r = self.jobrun("n2", "--", "no-such-command-xyz", "arg")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.status("n2")["state"], "failed")
