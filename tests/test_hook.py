import json
import subprocess
import sys
import unittest

from tests.base import HOOK


class LaunchHook(unittest.TestCase):
    def fires(self, command):
        payload = json.dumps({"tool_input": {"command": command}})
        r = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_a_launch_fires_and_the_rest_is_silent(self):
        self.assertIn("jobwarden wait", self.fires("jobwarden run ft1 --host box1 -- train"))
        for command in ("jobwarden watch x", "jobwarden wait x", "ls | head", "jobwarden run --help", 'grep "jobwarden run" README.md'):
            self.assertEqual(self.fires(command), "", command)

    def test_bad_payload_is_silent(self):
        r = subprocess.run([sys.executable, HOOK], input="not json", capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
