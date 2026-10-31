import os
import subprocess

from tests.base import JOBWARDEN, Base


class Demo(Base):
    def test_it_wakes_on_the_check_and_cleans_up(self):
        r = subprocess.run([*JOBWARDEN, "demo"], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("step", r.stdout)
        self.assertIn("JOB jobwarden-demo CHECK: loss is NaN", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".jobwarden", "jobs", "jobwarden-demo")))
