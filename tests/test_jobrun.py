import os
import signal
import subprocess
import sys
import time

from jobwarden import reader
from tests.base import JOBRUN, UUID_A, UUID_B, Base, alive, load_jobrun, wait_for, write_exe


class Supervisor(Base):
    def kill_supervisor(self, name):
        st = self.wait_state(name, "running")
        os.kill(st["pid"], signal.SIGKILL)
        return st

    def test_job_survives_supervisor_and_is_shown_as_such(self):
        self.jobrun("o", "--", "sleep 20")
        st = self.kill_supervisor("o")
        self.env["JOBWARDEN_STALE"] = "1"
        time.sleep(3)
        r = self.watch_once("lo", "o")
        self.assertIn("orphan", r.stdout)
        self.assertIn("job still alive", r.stdout)
        self.assertTrue(alive(st["job_pid"]))
        r2 = self.jobrun("o", "--", "true")
        self.assertEqual(r2.returncode, 4)
        self.assertIn("still alive", r2.stderr)
        os.kill(st["job_pid"], signal.SIGKILL)

    def test_reboot_is_failed_with_unknown_exit_code(self):
        self.jobrun("o2", "--", "sleep 1")
        st = self.kill_supervisor("o2")
        wait_for(lambda: not alive(st["job_pid"]), 10, "job end")
        self.env["JOBWARDEN_STALE"] = "0.5"
        time.sleep(3)
        r = self.watch_once("lo", "o2")
        self.assertIn("exit code unknown", r.stdout)

    def test_signals_do_not_kill_the_supervisor(self):
        self.jobrun("o3", "--", "sleep 3; exit 4")
        st = self.wait_state("o3", "running")
        os.kill(st["pid"], signal.SIGHUP)
        os.kill(st["pid"], signal.SIGTERM)
        self.assertEqual(self.wait_state("o3", "failed")["exit_code"], 4)

    def test_launching_process_holds_nothing_open(self):
        """Pipes given to jobrun must close as soon as it returns, or ssh would wait for the whole job."""
        t0 = time.time()
        r = subprocess.run(
            [sys.executable, JOBRUN, "start", "p", "--", "sleep 20"], env=self.env, capture_output=True, timeout=10
        )
        self.assertEqual(r.returncode, 0)
        self.assertLess(time.time() - t0, 5)

    def test_same_name_is_refused_while_running(self):
        self.jobrun("a", "--", "sleep 5")
        r = self.jobrun("a", "--", "true")
        self.assertEqual(r.returncode, 4)
        self.assertIn("already running", r.stderr)

    def test_rerun_keeps_the_previous_log(self):
        self.jobrun("b", "--", "echo first")
        self.wait_state("b", "done")
        wait_for(lambda: self.jobrun("b", "--", "echo second").returncode == 0, 5, "lock release")
        self.wait_state("b", "done")
        self.assertEqual(self.log("b").strip(), "second")
        prev = os.path.join(self.home, "jobs", "b", "prev")
        with open(os.path.join(prev, os.listdir(prev)[0], "log")) as f:
            self.assertEqual(f.read().strip(), "first")

    def test_bad_name_and_empty_command_refused(self):
        self.assertEqual(self.jobrun("../x", "--", "true").returncode, 2)
        self.assertEqual(self.jobrun("x", "--").returncode, 2)


class Progress(Base):
    def test_parsing(self):
        jr = load_jobrun()
        p = jr.parse_progress("12:01:02 step 120/513 loss 0.85 lr=1e-4 val_loss: 0.9 1.2s/step")
        self.assertEqual((p["clock"], p["unit"], p["step"], p["total"]), ("12:01:02", "step", 120, 513))
        self.assertEqual(p["metrics"], {"loss": "0.85", "lr": "1e-4", "val_loss": "0.9"})
        self.assertEqual(p["s_per_step"], 1.2)
        self.assertEqual(jr.parse_progress("12:01:02 turn 4/9")["unit"], "turn")
        self.assertEqual(jr.parse_progress("\x1b[32m12:01:02 step 4/9\x1b[0m")["step"], 4)
        self.assertEqual(jr.parse_progress("12:01:02 stage gguf")["stage"], "gguf")
        self.assertIsNone(jr.parse_progress("Loading 3/4 shards"))
        self.assertIsNone(jr.parse_progress("12:01:02 nothing here"))

    def test_status_follows_the_log(self):
        self.jobrun(
            "g",
            "--",
            "echo '23:59:01 step 1/4 loss 0.9'; sleep 1; echo '23:59:02 step 2/4 loss 0.8'; "
            "printf '23:59:03 step 3/4 loss 0.7\\r'; printf 'tail'; sleep 2; echo; echo '23:59:03 stage gguf'; sleep 1",
        )
        st = wait_for(lambda: (s := self.status("g"))["progress"] and s["progress"]["step"] == 3 and s, 15, "step 3")
        self.assertEqual(st["progress"]["metrics"], {"loss": "0.7"})
        self.assertIsNotNone(st["progress"]["s_per_step"])
        self.assertAlmostEqual(st["progress_at"], time.time(), delta=5)  # host clock, whatever the line's clock says
        st = self.wait_state("g", "done")
        self.assertEqual(st["stage"], "gguf")

    def test_utc_log_clock_is_not_a_stall(self):
        """Containers log in UTC: the stamp in the line can be hours off, the time it was seen can't."""
        self.jobrun("u", "--", "echo '00:00:01 step 1/4'; sleep 15")
        self.watch("lo", "u")
        wait_for(lambda: (d := self.dock("u")) and d.get("total") == 4, 10, "progress")
        d = self.dock("u")
        self.assertFalse(reader.is_stalled(d))
        self.assertLess(abs(self.status("u")["progress_at"] - time.time()), 10)

    def test_frame_has_what_the_tab_must_show(self):
        cfg = os.path.join(self.tmp, "cfg.py")
        with open(cfg, "w") as f:
            f.write(
                'SUMMARY = "What it is for\\nWhere results go"\nMETRICS = ["loss"]\n'
                'def results(job): return ["results for " + job["name"]]\n'
            )
        self.jobrun("f", "--gpu", "2060", "--", "echo '10:00:00 step 5/10 loss 0.5 acc 3 2s/step'; sleep 20")
        wait_for(lambda: self.status("f")["progress"], 10, "progress")
        out = self.watch_once("lo", "f", "--config", cfg).stdout
        for want in (
            "What it is for",
            "5/10",
            "ETA 10s",
            "loss 0.5",
            f"GPU 2060 ({UUID_B})",
            "last progress",
            "polled",
            "results for f",
            "█",
        ):
            self.assertIn(want, out)
        self.assertNotIn("acc 3", out)

    def test_failing_or_slow_hook_cannot_take_the_watcher_down(self):
        cfg = os.path.join(self.tmp, "cfg.py")
        with open(cfg, "w") as f:
            f.write("def results(job): raise ValueError('boom')\n")
        self.jobrun("h", "--", "sleep 5")
        r = self.watch_once("lo", "h", "--config", cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("boom", r.stdout)

    def test_log_cannot_drive_the_terminal(self):
        self.jobrun("x", "--", r"printf '\033]0;pwned\007\033[2Jevil\n'; exit 1")
        self.wait_state("x", "failed")
        out = self.watch_once("lo", "x").stdout
        self.assertIn("evil", out)
        self.assertNotIn("\x07", out)
        self.assertNotIn("\x1b]", out)

    def test_several_jobs_in_one_tab(self):
        self.jobrun("m1", "--", "true")
        self.jobrun("m2", "--", "exit 3")
        w = self.watch("lo", "m1", "m2")
        w.wait(timeout=20)
        self.assertEqual(self.dock("m1")["state"], "done")
        self.assertEqual(self.dock("m2")["state"], "failed")

    def test_unknown_job(self):
        r = self.watch_once("lo", "nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no job nope", r.stderr)


class Gpu(Base):
    def env_of(self, name):
        """What the job saw."""
        with open(os.path.join(self.tmp, name)) as f:
            return f.read().strip()

    def test_plain_process_gets_the_uuid(self):
        out = os.path.join(self.tmp, "seen")
        self.jobrun("g1", "--gpu", "2060", "--", f'echo "$CUDA_VISIBLE_DEVICES $JOB_GPU" > {out}')
        st = self.wait_state("g1", "done")
        self.assertEqual(self.env_of("seen"), f"{UUID_B} {UUID_B}")
        self.assertEqual(st["gpu"], {"name": "2060", "uuid": UUID_B, "forced": False})

    def test_no_gpu_means_no_gpu(self):
        out = os.path.join(self.tmp, "seen")
        self.jobrun("g2", "--", f'echo "[$CUDA_VISIBLE_DEVICES]" > {out}')
        self.assertIsNone(self.wait_state("g2", "done")["gpu"])
        self.assertEqual(self.env_of("seen"), "[]")

    def test_docker_command_is_not_rewritten_and_gets_job_gpu(self):
        out = os.path.join(self.tmp, "docker-args")
        write_exe(os.path.join(self.bin, "docker"), f'#!/bin/sh\necho "$@" > {out}\n')
        cmd = 'docker run --rm --name "$JOB_CONTAINER" --gpus "device=$JOB_GPU" img python train.py'
        self.jobrun("d1", "--gpu", "2080", "--", cmd)
        st = self.wait_state("d1", "done", "failed")
        self.assertEqual(st["state"], "done", st["reason"])
        self.assertEqual(
            self.env_of("docker-args"), f"run --rm --name job-d1 --gpus device={UUID_A} img python train.py"
        )
        self.assertEqual(st["cmd"], cmd)

    def test_docker_without_job_gpu_refused(self):
        r = self.jobrun("d2", "--gpu", "2080", "--", "docker run --gpus all img")
        self.assertEqual(r.returncode, 2)
        self.assertIn("$JOB_GPU", r.stderr)

    def test_docker_gpu_expanded_too_early_refused(self):
        r = self.jobrun("d2b", "--gpu", "2080", "--", "docker", "run", "--gpus", "device=", "img")
        self.assertEqual(r.returncode, 2)

    def test_docker_detached_refused(self):
        for flag in ("-d", "-itd", "--detach"):
            r = self.jobrun("d3", "--", f"docker run {flag} img")
            self.assertEqual(r.returncode, 2, flag)
            self.assertIn("reported finished", r.stderr)
        r = self.jobrun("d3b", "--", 'docker run --rm --name "$JOB_CONTAINER" img train.py -data x')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_docker_taking_gpus_without_reservation_refused(self):
        r = self.jobrun("d4", "--", "docker run --gpus all img")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no card would be reserved", r.stderr)

    def test_busy_card_refused_with_pid_process_memory(self):
        self.set_gpus([(UUID_A, "RTX 2080", 5000), (UUID_B, "RTX 2060", 10)], [(UUID_A, 4242, "python3", 4900)])
        r = self.jobrun("b1", "--gpu", "2080", "--", "true")
        self.assertEqual(r.returncode, 5)
        for want in ("pid 4242", "python3", "4900 MiB", "--wait-gpu", "--force", UUID_A):
            self.assertIn(want, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs/b1/status.json")))

    def test_memory_without_process_counts_as_busy(self):
        self.set_gpus([(UUID_A, "RTX 2080", 5000)], [])
        self.assertEqual(self.jobrun("b2", "--gpu", "2080", "--", "true").returncode, 5)

    def test_a_little_display_memory_is_free(self):
        self.set_gpus([(UUID_A, "RTX 2080", 300)], [])
        self.assertEqual(self.jobrun("b3", "--gpu", "2080", "--", "true").returncode, 0)

    def test_force_overrides_and_is_recorded(self):
        self.set_gpus([(UUID_A, "RTX 2080", 5000)], [(UUID_A, 4242, "python3", 4900)])
        self.assertEqual(self.jobrun("b4", "--gpu", "2080", "--force", "--", "true").returncode, 0)
        self.assertTrue(self.wait_state("b4", "done")["gpu"]["forced"])

    def test_wait_gpu_queues_then_starts(self):
        self.set_gpus([(UUID_A, "RTX 2080", 5000)], [(UUID_A, 4242, "python3", 4900)])
        r = self.jobrun("q1", "--gpu", "2080", "--wait-gpu", "--", "echo ran")
        self.assertEqual(r.returncode, 0, r.stderr)
        st = wait_for(
            lambda: (x := self.wait_state("q1", "queued")) and "pid 4242" in (x["reason"] or "") and x, 5, "reason"
        )
        time.sleep(0.6)
        self.assertEqual(self.status("q1")["state"], "queued")
        self.set_gpus([(UUID_A, "RTX 2080", 10)], [])
        self.wait_state("q1", "done")
        self.assertIn("ran", self.log("q1"))

    def test_any_takes_the_first_free_card(self):
        self.set_gpus([(UUID_A, "RTX 2080", 5000), (UUID_B, "RTX 2060", 10)], [(UUID_A, 1, "x", 4900)])
        self.jobrun("a1", "--gpu", "any", "--", "true")
        self.assertEqual(self.wait_state("a1", "done")["gpu"]["name"], "2060")

    def test_any_all_busy_refused(self):
        self.set_gpus([(UUID_A, "RTX 2080", 5000)], [(UUID_A, 1, "x", 4900)])
        self.assertEqual(self.jobrun("a2", "--gpu", "any", "--", "true").returncode, 5)

    def test_card_stays_reserved_before_the_model_has_loaded(self):
        """nvidia-smi shows nothing for the first minute of a job: two launches must still not share the card."""
        self.assertEqual(self.jobrun("r1", "--gpu", "2080", "--", "sleep 6").returncode, 0)
        r = self.jobrun("r2", "--gpu", "2080", "--", "true")
        self.assertEqual(r.returncode, 5)
        self.assertIn("reserved by jobrun job r1", r.stderr)
        self.jobrun("r3", "--gpu", "any", "--", "true")
        self.assertEqual(self.wait_state("r3", "done")["gpu"]["name"], "2060")

    def test_card_is_released_when_the_job_ends(self):
        self.jobrun("r4", "--gpu", "2080", "--", "true")
        self.wait_state("r4", "done")
        wait_for(lambda: self.jobrun("r5", "--gpu", "2080", "--", "true").returncode == 0, 5, "release")

    def test_short_names_not_index(self):
        jr = load_jobrun()
        self.assertEqual(
            jr.short_names(
                [
                    {"name": "NVIDIA GeForce RTX 2060", "uuid": "a"},
                    {"name": "NVIDIA GeForce RTX 2080 SUPER", "uuid": "b"},
                ]
            ),
            {"2060": "a", "2080": "b"},
        )
        self.assertEqual(
            jr.short_names([{"name": "RTX 3090", "uuid": "a"}, {"name": "RTX 3090", "uuid": "b"}]),
            {"3090-0": "a", "3090-1": "b"},
        )
        self.assertEqual(jr.short_names([{"name": "NVIDIA RTX A6000", "uuid": "a"}]), {"a6000": "a"})

    def test_unknown_gpu_refused(self):
        r = self.jobrun("u1", "--gpu", "3090", "--", "true")
        self.assertEqual(r.returncode, 5)
        self.assertIn("2080", r.stderr)

    def test_no_nvidia_smi_refused(self):
        os.remove(os.path.join(self.bin, "nvidia-smi"))
        r = self.jobrun("u2", "--gpu", "any", "--", "true")
        self.assertEqual(r.returncode, 5)


class CommandFlags(Base):
    def test_flags_after_the_separator_belong_to_the_job(self):
        r = self.jobrun("df", "--", "echo", "--discover-gpus")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_state("df", "done")
        self.assertIn("--discover-gpus", self.log("df"))
