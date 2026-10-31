import os
import shutil
import socket
import subprocess
import time
import unittest

from tests.base import JOBWARDEN, UUID_A, UUID_B, Base, wait_for


def have_sshd():
    return os.path.exists("/usr/sbin/sshd") and shutil.which("ssh-keygen")


@unittest.skipUnless(have_sshd(), "no sshd on this machine")
class Pod(Base):
    """A real sshd on a custom port, HOME forced to a temp dir, no docker: what a RunPod pod looks like."""

    def setUp(self):
        super().setUp()
        self.sshd_dir = os.path.join(self.tmp, "d")
        os.makedirs(self.sshd_dir, mode=0o700)
        d = self.sshd_dir
        for name in ("host", "client"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", f"{d}/{name}"], check=True)
        shutil.copy(f"{d}/client.pub", f"{d}/authorized_keys")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        force = f'env HOME={self.home} PATH={self.bin}:/opt/homebrew/bin:/usr/bin:/bin sh -c "$SSH_ORIGINAL_COMMAND"'
        with open(f"{d}/sshd_config", "w") as f:
            f.write(
                f"Port {self.port}\nListenAddress 127.0.0.1\nHostKey {d}/host\nAuthorizedKeysFile {d}/authorized_keys\n"
                f"PidFile {d}/pid\nStrictModes no\nUsePAM no\nPasswordAuthentication no\nLogLevel VERBOSE\n"
                f"ForceCommand {force}\n"
            )
        self.sshd_log = f"{d}/log"
        self.start_sshd()
        self.save_hosts({"pod": self.pod_conf()})

    def cleanup(self):
        subprocess.run(["pkill", "-f", f"ssh.*{self.tmp}"], capture_output=True)  # ControlPersist masters
        super().cleanup()

    def pod_conf(self):
        return {
            "ssh": f"{os.environ['USER']}@127.0.0.1",
            "port": self.port,
            "temporary": True,
            "extra": f"-F /dev/null -i {self.sshd_dir}/client -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile={self.sshd_dir}/known",
        }

    def start_sshd(self):
        self.sshd = subprocess.Popen(
            ["/usr/sbin/sshd", "-D", "-f", f"{self.sshd_dir}/sshd_config", "-E", self.sshd_log],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(self.sshd)

        def up():
            with socket.socket() as s:
                return s.connect_ex(("127.0.0.1", self.port)) == 0

        try:
            wait_for(up, 5, "sshd")
        except AssertionError:
            self.skipTest("sshd can't run here")

    def accepted(self):
        with open(self.sshd_log) as f:
            return f.read().count("Accepted publickey")

    def jobwarden(self, *args):
        return subprocess.run([*JOBWARDEN, *args], env=self.env, capture_output=True, text=True, timeout=60)

    def install(self):
        r = self.jobwarden("install", "pod")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_install_discovers_gpus_into_short_names(self):
        r = self.install()
        self.assertIn(f"2080 = {UUID_A}", r.stdout)
        h = self.hosts_file()["pod"]
        self.assertEqual(h["gpus"], {"2080": UUID_A, "2060": UUID_B})
        self.assertTrue(os.access(os.path.join(self.home, ".local/bin/jobrun"), os.X_OK))

    def test_run_warns_when_the_hosts_jobrun_is_out_of_date(self):
        self.install()
        self.assertNotIn("differs", self.jobwarden("run", "v1", "--host", "pod", "--", "true").stderr)
        with open(os.path.join(self.home, ".local/bin/jobrun"), "a") as f:
            f.write("\n# older\n")
        r = self.jobwarden("run", "v2", "--host", "pod", "--", "true")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("jobwarden install pod", r.stderr)

    def test_polls_share_one_connection(self):
        self.install()
        self.assertEqual(self.jobwarden("run", "j", "--host", "pod", "--", "sleep 30").returncode, 0)
        before = self.accepted()
        for _ in range(5):
            r = self.watch_once("pod", "j")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.accepted(), before, "polls opened new SSH connections")

    def test_lost_host_is_unreachable_then_recovers(self):
        self.install()
        self.jobwarden("run", "j2", "--host", "pod", "--", "sleep 60")
        self.env.update(JOBWARDEN_BACKOFF="0.1", JOBWARDEN_SSH_TIMEOUT="3")
        self.assertEqual(self.watch_once("pod", "j2").returncode, 0)
        self.sshd.terminate()
        self.sshd.wait()
        # sshd's per-connection child (and so the master) outlives the listener; kill it as a lost host would
        subprocess.run(["pkill", "-f", f"{self.sshd_dir}/sshd_config"], capture_output=True)
        subprocess.run(["pkill", "-f", f"ssh.*-S {self.env['JOBWARDEN_SSH_DIR']}"], capture_output=True)
        wait_for(lambda: self.watch_once("pod", "j2").returncode == 3, 20, "unreachable")
        self.start_sshd()
        wait_for(lambda: self.watch_once("pod", "j2").returncode == 0, 20, "recovery")

    def test_job_ending_on_temporary_host_says_it_may_be_billing(self):
        self.install()
        self.jobwarden("run", "j3", "--host", "pod", "--", "true")
        w = self.waiter("j3", "--host", "pod")
        out, _ = w.communicate(timeout=10)
        self.assertEqual(w.returncode, 0)
        self.assertIn("may still be billing", out)

    def test_launch_returns_at_once_and_job_survives_disconnect(self):
        self.install()
        t0 = time.time()
        r = self.jobwarden("run", "j4", "--host", "pod", "--", "sleep 3; echo survived")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.time() - t0, 3)
        subprocess.run(["pkill", "-f", f"ssh.*-S {self.env['JOBWARDEN_SSH_DIR']}"], capture_output=True)
        self.wait_state("j4", "done")
        self.assertIn("survived", self.log("j4"))

    def test_same_name_on_two_hosts_needs_a_host(self):
        """The stand-in pod shares its HOME with this machine, so every job here is on two hosts."""
        self.install()
        self.jobwarden("run", "dup", "--host", "pod", "--", "sleep 2")
        w = self.waiter("dup")
        out, _ = w.communicate(timeout=20)
        self.assertEqual(w.returncode, 4, out)
        self.assertIn("--host", out)
        self.assertEqual(self.watch_once("pod", "dup").returncode, 0)

    def test_real_ssh_refusal_comes_back_with_its_exit_code(self):
        self.install()
        self.set_gpus([(UUID_A, "RTX 2080", 5000)], [(UUID_A, 77, "python3", 5000)])
        r = self.jobwarden("run", "j5", "--host", "pod", "--gpu", "2080", "--", "true")
        self.assertEqual(r.returncode, 5)
        self.assertIn("pid 77 python3 5000 MiB", r.stderr)
