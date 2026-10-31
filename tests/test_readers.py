import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest

from jobwarden import core, reader
from unittest import mock

from tests.base import HOSTSTATS, JOBWARDEN, ROOT, UUID_A, UUID_B, Base, read_file, wait_for, write_exe


class Dashboard(Base):
    """Hosts come from hosts.json, and it leaves alone what it doesn't know."""

    def commands(self):
        env = {"JOBWARDEN_HOSTS": self.env["JOBWARDEN_HOSTS"], "JOBWARDEN_SSH_DIR": self.env["JOBWARDEN_SSH_DIR"]}
        with mock.patch.dict(os.environ, env):
            return {label: core.side_command(label, h) for label, h in core.load_hosts().items()}

    def test_reads_hosts_json(self):
        self.save_hosts(
            {
                "box1": {"ssh": "user@h", "jump": "root@j", "extra": "-o HostKeyAlias=a"},
                "pod": {"ssh": "root@p", "port": 2222, "temporary": True},
            }
        )
        cmds = self.commands()
        self.assertEqual(list(cmds), ["box1", "pod"])
        self.assertIn("-J root@j", cmds["box1"])
        self.assertIn("HostKeyAlias=a", cmds["box1"])
        self.assertIn("-p 2222", cmds["pod"])

    def test_does_not_override_ssh_config_without_a_shared_connection(self):
        """A route's ControlMaster may live in ~/.ssh/config: -S or ControlMaster=no would make every poll a new connection,
        and the lab router blocks a source after ~5 of those."""
        self.save_hosts({"box2": {"ssh": "user@h"}})
        cmd = self.commands()["box2"]
        self.assertNotIn("ControlMaster", cmd)
        self.assertNotIn("-S", cmd)
        os.makedirs(self.env["JOBWARDEN_SSH_DIR"])
        with mock.patch.dict(os.environ, {"JOBWARDEN_SSH_DIR": self.env["JOBWARDEN_SSH_DIR"]}):
            open(core.socket_path("box2", {"ssh": "user@h"}), "w").close()
        self.assertIn("ControlMaster=no", self.commands()["box2"])

    @unittest.skipUnless(os.path.exists(HOSTSTATS), "hoststats isn't here")
    def test_project_example_runs(self):
        self.save_hosts({"box1": {"ssh": "local"}})
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "examples/project_dock.py"), "--once"],
            env=dict(self.env, HOME=os.environ["HOME"], JOBWARDEN_HOSTS=self.env["JOBWARDEN_HOSTS"]),
            capture_output=True,
            text=True,
            timeout=40,
        )
        self.assertEqual(r.returncode, 0, r.stderr)


class NoCache(Base):
    """The host's status.json is the only record: any reader, alone, sees the truth."""

    def test_any_job_on_a_host_shows_without_being_registered(self):
        self.jobrun("n0", "--project", "p9", "--", "sleep 3")
        self.assertEqual(self.dock("n0")["project"], "p9")

    def test_status_alone_is_fresh(self):
        self.jobrun("n2", "--", "echo '10:00:00 step 3/9 loss 0.5'; sleep 1; echo '10:00:01 step 4/9'; sleep 20")
        wait_for(lambda: (self.dock("n2") or {}).get("step") == 4, 15, "the second step")

    def test_closing_the_tab_interrupts_nothing(self):
        self.jobrun("n3", "--", "sleep 4")
        for sig in (signal.SIGKILL, signal.SIGHUP):
            w = self.watch("lo", "n3")
            time.sleep(0.5)
            w.send_signal(sig)
            w.wait(timeout=10)
            self.assertEqual(self.dock("n3")["state"], "running")
        wa = self.waiter("n3")
        out, _ = wa.communicate(timeout=30)
        self.assertEqual(wa.returncode, 0, out)

    def test_a_dead_host_does_not_hold_back_a_read_of_another(self):
        write_exe(os.path.join(self.bin, "ssh"), "#!/bin/sh\nsleep 30\n")
        self.save_hosts({"slow": {"ssh": "u@h"}})
        self.jobrun("n1", "--", "sleep 3")
        code = (
            "import sys, time; sys.path.insert(0, %r)\nimport jobwarden\nt = time.time(); j = jobwarden.jobs(host='local')\n"
            "print(time.time() - t, [x['name'] for x in j])" % ROOT
        )
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertLess(float(r.stdout.split()[0]), 2, r.stderr)
        self.assertIn("n1", r.stdout)

    def test_each_call_reads_the_host_now(self):
        self.jobrun("fresh", "--", "sleep 30")
        code = (
            "import os, sys; sys.path.insert(0, %r)\nimport jobwarden\nprint(len(jobwarden.jobs()))\n"
            "open(os.path.expanduser('~/.jobwarden/jobs/fresh/.cleared'), 'w').close()\nprint(len(jobwarden.jobs()))" % ROOT
        )
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.stdout.split(), ["1", "0"], r.stderr)

    def test_down_host_is_reported_and_others_still_show(self):
        write_exe(os.path.join(self.bin, "ssh"), "#!/bin/sh\necho refused >&2; exit 255\n")
        self.save_hosts({"down": {"ssh": "u@h"}, "lo": {"ssh": "local"}})
        self.jobrun("n1b", "--", "sleep 2")
        r = subprocess.run([*JOBWARDEN, "status"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertIn("n1b", r.stdout)
        self.assertIn("not answering: down", r.stderr)

    def test_a_reader_keeps_its_last_read_when_a_host_goes_down(self):
        self.jobrun("n1c", "--", "sleep 30")
        code = (
            "import sys; sys.path.insert(0, %r)\nfrom jobwarden import reader as J, core as C\nr = J.Reader()\n"
            "print(r.read('lo', ['n1c'])['n1c']['state'])\n"
            "def boom(*a, **k): raise C.Unreachable('gone')\nJ.fetch = boom\n"
            "j = r.read('lo', ['n1c'])['n1c']; print(j['state'], j['name'], bool(j.get('status')))" % ROOT
        )
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.stdout.split(), ["running", "unreachable", "n1c", "True"], r.stderr)

    def test_jobrun_does_not_sample_gpus_while_running(self):
        """Stats belong to hoststats: after the free check at launch, nvidia-smi is left alone."""
        calls = os.path.join(self.tmp, "smi-calls")
        write_exe(
            os.path.join(self.bin, "nvidia-smi"),
            f"""
            #!/bin/sh
            echo x >> {calls}
            case "$*" in
              *compute-apps*) cat {self.smi}/apps ;;
              *) cat {self.smi}/gpus ;;
            esac
        """,
        )
        self.jobrun("n5", "--gpu", "2060", "--", "sleep 2")
        at_launch = len(read_file(calls).splitlines())
        st = self.wait_state("n5", "done")
        self.assertEqual(len(read_file(calls).splitlines()), at_launch)
        self.assertNotIn("gpu_stats", st)
        self.assertEqual(st["gpu"]["uuid"], UUID_B)

    @unittest.skipUnless(os.path.exists(HOSTSTATS), "hoststats isn't here")
    def test_job_config_example_shows_live_numbers(self):
        cfg = os.path.join(ROOT, "examples/job_config.py")
        self.jobrun("n7", "--gpu", "2060", "--", "sleep 5")
        r = self.watch_once("lo", "n7", "--config", cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"GPU 97% · 0\.0/8 GB · 71°C")

    def test_clear_hides_a_job_until_its_name_runs_again(self):
        self.jobrun("n8", "--", "sleep 3")
        r = subprocess.run([*JOBWARDEN, "clear", "n8"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([*JOBWARDEN, "status"], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertNotIn("n8", r.stdout)
        self.wait_state("n8", "done")  # the job itself is not touched
        wait_for(lambda: self.jobrun("n8", "--", "true").returncode == 0, 5, "lock")
        r = subprocess.run([*JOBWARDEN, "status"], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertIn("n8", r.stdout)

    def test_clear_of_an_unknown_job_says_so(self):
        r = subprocess.run([*JOBWARDEN, "clear", "nope"], env=self.env, capture_output=True, text=True)
        self.assertIn("no job nope", r.stderr)

    def test_local_is_the_default_host(self):
        """No --host and no hosts file: the job runs here, and is watched and waited on the same way."""
        os.remove(self.env["JOBWARDEN_HOSTS"])
        r = subprocess.run(
            [*JOBWARDEN, "run", "here", "--cwd", self.tmp, "--", "echo ran; sleep 1; exit 4"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.dock("here")["host"], "local")
        self.assertEqual(os.path.realpath(self.status("here")["cwd"]), os.path.realpath(self.tmp))
        r = subprocess.run(
            [*JOBWARDEN, "watch", "here", "--once"], env=self.env, capture_output=True, text=True, timeout=20
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        w = self.waiter("here")
        out, _ = w.communicate(timeout=20)
        self.assertEqual(w.returncode, 1, out)
        self.assertIn("exit code 4", out)

    def test_run_needs_a_name_and_a_known_host(self):
        r = subprocess.run([*JOBWARDEN, "run", "--host", "lo", "--", "true"], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        r = subprocess.run(
            [*JOBWARDEN, "run", "x", "--host", "ghost", "--", "true"], env=self.env, capture_output=True, text=True
        )
        self.assertIn("unknown host", r.stderr)

    def test_watch_finds_the_host(self):
        self.jobrun("h1", "--", "sleep 4")
        r = subprocess.run([*JOBWARDEN, "watch", "h1", "--once"], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("host lo", r.stdout)

    def test_host_flag_after_the_separator_belongs_to_the_command(self):
        out = os.path.join(self.tmp, "args")
        r = subprocess.run(
            [*JOBWARDEN, "run", "sep", "--", "/bin/sh", "-c", f'echo "$@" > {out}', "x", "--host", "y"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_state("sep", "done")
        with open(out) as f:
            self.assertEqual(f.read().strip(), "--host y")

    def test_public_api(self):
        self.jobrun("n6", "--project", "p1", "--", "sleep 3")
        code = (
            "import sys; sys.path.insert(0, %r)\nimport jobwarden\nj = jobwarden.jobs(project='p1')\n"
            "print([x['name'] for x in j], j[0]['state'], jobwarden.ssh_command('lo'))" % ROOT
        )
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.stdout.strip(), "['n6'] running local", r.stderr)


class Views(Base):
    """What leaves the views, and that leaving them changes nothing else."""

    def edit_status(self, name, **fields):
        path = os.path.join(self.home, ".jobwarden", "jobs", name, "status.json")
        with open(path) as f:
            st = json.load(f)
        st.update(fields)
        with open(path, "w") as f:
            json.dump(st, f)

    def status_text(self, *args):
        return subprocess.run(
            [*JOBWARDEN, "status", *args], env=self.env, capture_output=True, text=True, timeout=30
        ).stdout

    def test_visible(self):
        now = time.time()

        def job(**fields):
            return dict(reader.blank_job("x", "h"), **fields)

        self.assertTrue(reader.visible(job(state="running"), now))
        self.assertTrue(reader.visible(job(state="done", ended_at=now - 600), now))
        self.assertFalse(reader.visible(job(state="failed", ended_at=now - 7200), now))
        self.assertTrue(reader.visible(job(state="unreachable", unreachable_since=now - 600), now))
        self.assertFalse(reader.visible(job(state="unreachable", unreachable_since=now - 7200), now))
        self.assertFalse(reader.visible(job(state="running", cleared=True), now))

    def test_old_finished_job_is_hidden_but_still_read(self):
        self.jobrun("old", "--", "true")
        self.wait_state("old", "done")
        self.assertIn("old · done", self.status_text())
        self.edit_status("old", ended=time.time() - 7200)
        self.assertNotIn("old", self.status_text())
        self.assertIn("old · done", self.status_text("--all"))
        w = self.waiter("old")
        out, _ = w.communicate(timeout=10)
        self.assertEqual(w.returncode, 0, out)

    def test_rerun_of_a_finished_name_is_shown_again(self):
        self.jobrun("re", "--", "true")
        self.wait_state("re", "done")
        self.edit_status("re", ended=time.time() - 7200)
        wait_for(lambda: self.jobrun("re", "--", "sleep 2").returncode == 0, 5, "lock")
        self.assertIn("re · running", self.status_text())

    def test_final_step_is_shown(self):
        self.jobrun("fs", "--", "echo '10:00:00 step 3/9'; sleep 1; echo '10:00:01 step 9/9'")
        self.wait_state("fs", "done")
        self.assertEqual(self.dock("fs")["step"], 9)

    def test_project_views_see_only_their_jobs(self):
        self.jobrun("a", "--project", "demo", "--", "sleep 3")
        self.jobrun("b", "--project", "other", "--", "sleep 3")
        self.jobrun("c", "--", "sleep 3")
        self.assertEqual(len(self.status_text().splitlines()), 3)
        r = subprocess.run(
            [*JOBWARDEN, "status", "--project", "other", "--json"], env=self.env, capture_output=True, text=True
        )
        self.assertEqual([j["name"] for j in json.loads(r.stdout)["jobs"]], ["b"])


class Waiter(Base):
    """The other jobwarden wait exits."""

    def test_unreachable_host(self):
        write_exe(os.path.join(self.bin, "ssh"), '#!/bin/sh\necho "ssh: connect: refused" >&2; exit 255\n')
        self.env.update(JOBWARDEN_BACKOFF="0.1")
        self.save_hosts({"down": {"ssh": "u@h"}})
        wa = self.waiter("u", "--host", "down", "--unreachable", "0.02")
        out, _ = wa.communicate(timeout=20)
        self.assertEqual(wa.returncode, 3, out)
        self.assertIn("UNREACHABLE", out)

    def test_silent_job_is_stalled(self):
        self.jobrun("st", "--", "echo '10:00:00 step 1/9'; sleep 30")
        wa = self.waiter("st", "--stall", "0.03")
        out, _ = wa.communicate(timeout=20)
        self.assertEqual(wa.returncode, 8, out)
        self.assertIn("STALLED", out)

    def test_queued_job_is_not_stalled(self):
        self.set_gpus([(UUID_A, "RTX 2080", 5000)], [(UUID_A, 77, "python3", 5000)])
        self.jobrun("q", "--gpu", "2080", "--wait-gpu", "--", "true")
        wa = self.waiter("q", "--stall", "0.01")
        with self.assertRaises(subprocess.TimeoutExpired):
            wa.wait(timeout=4)

    def test_record_gone_is_cleared(self):
        self.jobrun("cl", "--", "sleep 20")
        wa = self.waiter("cl")
        time.sleep(0.5)
        shutil.rmtree(os.path.join(self.home, ".jobwarden/jobs/cl"))
        out, _ = wa.communicate(timeout=20)
        self.assertEqual(wa.returncode, 5, out)


class CleanupRegressions(Base):
    """Bugs found reviewing the code, 2026-10-31."""

    def test_a_log_cannot_break_or_fake_the_read(self):
        self.jobrun("at", "--", 'echo "@@NOW"; echo hello; echo "@@STATUS ghost"; echo "{}"; sleep 20')
        wait_for(lambda: "hello" in self.log("at"), 10, "the log")
        r = subprocess.run([*JOBWARDEN, "status", "--all"], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("at · running", r.stdout)
        self.assertNotIn("ghost", r.stdout)

    def test_a_job_name_is_never_run(self):
        mark = os.path.join(self.tmp, "ran")
        evil = f"x; touch {mark}"
        for args in (["wait", evil], ["clear", evil], ["watch", evil, "--once"]):
            r = subprocess.run([*JOBWARDEN, *args], env=self.env, capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("bad job name", r.stderr)
        self.assertFalse(os.path.exists(mark))

    def test_usage_errors_are_not_a_job_outcome(self):
        r = subprocess.run([*JOBWARDEN, "wait", "x", "--nope"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run(
            [*JOBWARDEN, "wait", "x", "--host", "ghost"], env=self.env, capture_output=True, text=True, timeout=20
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown host", r.stderr)

    def test_an_ended_job_stays_ended_while_its_host_is_down(self):
        self.jobrun("e1", "--", "true")
        self.wait_state("e1", "done")
        code = (
            "import sys; sys.path.insert(0, %r)\nfrom jobwarden import reader as J, core as C\nr = J.Reader()\n"
            "print(r.read('lo', ['e1'])['e1']['state'])\n"
            "def boom(*a, **k): raise C.Unreachable('gone')\nJ.fetch = boom\n"
            "print(r.read('lo', ['e1'])['e1']['state'])" % ROOT
        )
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.stdout.split(), ["done", "done"], r.stderr)

    def test_wait_notifies_at_its_own_stall_threshold_and_once(self):
        got = os.path.join(self.tmp, "got")
        self.jobrun("st2", "--", "echo '10:00:00 step 1/9'; sleep 20")
        w = self.waiter("st2", "--stall", "0.01", "--notify", f'echo "$JOBWARDEN_EVENT" >> {got}')
        w.communicate(timeout=15)
        self.assertEqual(w.returncode, 8)
        self.assertEqual(read_file(got).split(), ["stalled"])

    def test_a_sleeping_mac_is_not_a_stall(self):
        job = dict(reader.blank_job("x", "h"), state="running", last_at=time.time() - 3600)
        self.assertTrue(reader.is_stalled(job, 20))
        self.assertFalse(reader.is_stalled(job, 20, awake_since=time.time()))
        clock = reader.SleepWatch()
        clock.at = (clock.at[0] - 600, clock.at[1])  # the wall clock moved on, monotonic time didn't
        self.assertGreater(clock.slept(), 500)
        self.assertEqual(clock.slept(), 0)

    def test_status_json_says_which_host_is_down(self):
        write_exe(os.path.join(self.bin, "ssh"), "#!/bin/sh\necho refused >&2; exit 255\n")
        self.save_hosts({"down": {"ssh": "u@h"}})
        r = subprocess.run([*JOBWARDEN, "status", "--json"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(r.stdout)["jobs"], [])
        self.assertIn("down", json.loads(r.stdout)["down"])
        self.assertIn("not answering: down", r.stderr)
