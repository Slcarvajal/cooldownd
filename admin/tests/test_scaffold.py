import os
import pwd
import subprocess
import unittest
import urllib.request

import tests
from modules import common


class Scaffold(unittest.TestCase):
    def test_home_is_temporary(self):
        self.assertEqual(os.environ["HOME"], tests.HOME)
        self.assertEqual(os.path.expanduser("~"), tests.HOME)
        self.assertTrue(os.path.basename(tests.HOME).startswith("homelab-test-"))
        self.assertNotEqual(tests.HOME, pwd.getpwuid(os.getuid()).pw_dir)
        self.assertTrue(common.TOKEN_PATH.startswith(tests.HOME))

    def test_no_admin_config(self):
        self.assertNotIn("ADMIN_CONFIG", os.environ)

    def test_urlopen_fails(self):
        with self.assertRaises(AssertionError):
            urllib.request.urlopen("http://127.0.0.1:1/")

    def test_commands_blocked(self):
        with self.assertRaises(PermissionError):
            subprocess.run(["true"])
        self.assertEqual(common.run("true"), "")
        self.assertIsNone(common.orca("status"))


if __name__ == "__main__":
    unittest.main()
